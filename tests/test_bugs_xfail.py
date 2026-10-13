# -*- coding: utf-8 -*-

"""Known bugs, recorded as strict xfail tests

Each test states the behaviour we want and fails on purpose until the bug in its reason is fixed. strict=True makes
the suite fail once one starts passing, so whoever fixes the bug has to remove its marker.
"""

import json

import pytest
from flask import Blueprint
from marshmallow_jsonapi import fields
from marshmallow_jsonapi.flask import Schema, Relationship
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, JSON
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from flask_rest_jsonapi import Api, ResourceList, ResourceDetail, ResourceRelationship

NUMERIC_IDS = 'relationship POST/DELETE ignore numeric ids'
FAILED_WRITE = 'failed writes leave the session broken or half-applied'
LIST_FIELD = 'get_nested_fields reads List.container, which marshmallow 3 does not have'

Base = declarative_base()


class BugGroup(Base):
    __tablename__ = 'bug_group'

    id = Column(Integer, primary_key=True)
    name = Column(String)
    members = relationship('BugUser', back_populates='group')


class BugUser(Base):
    __tablename__ = 'bug_user'

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    nickname = Column(String)
    aliases = Column(JSON)
    group_id = Column(Integer, ForeignKey('bug_group.id'))
    group = relationship('BugGroup', back_populates='members')


class BugUserSchema(Schema):
    class Meta:
        type_ = 'bug_user'
        self_view = 'api.bug_user_detail'
        self_view_kwargs = {'id': '<id>'}

    id = fields.Integer(as_string=True)
    name = fields.Str(required=True)
    nickname = fields.Str()
    group = Relationship(related_view='api.bug_group_detail',
                         related_view_kwargs={'id': '<group_id>'},
                         schema='BugGroupSchema',
                         type_='bug_group')


class BugUserWithListSchema(BugUserSchema):
    class Meta:
        type_ = 'bug_user_with_list'
        self_view = 'api.bug_user_detail'
        self_view_kwargs = {'id': '<id>'}

    aliases = fields.List(fields.Str())


class BugGroupSchema(Schema):
    class Meta:
        type_ = 'bug_group'
        self_view = 'api.bug_group_detail'
        self_view_kwargs = {'id': '<id>'}

    id = fields.Integer(as_string=True)
    name = fields.Str()
    members = Relationship(related_view='api.bug_user_list',
                           schema='BugUserSchema',
                           type_='bug_user',
                           many=True)


@pytest.fixture()
def session():
    # a fresh database per test: these tests write, and some leave the session in a bad state on purpose
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    admins = BugGroup(name='admins')
    users = BugGroup(name='users')
    session.add_all([BugUser(name='alice', nickname='alice', group=admins),
                     BugUser(name='bob', nickname='bobby', group=users)])
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def client(app, session):
    class UserList(ResourceList):
        schema = BugUserSchema
        data_layer = {'session': session, 'model': BugUser}

    class UserDetail(ResourceDetail):
        schema = BugUserSchema
        data_layer = {'session': session, 'model': BugUser}

    class UserWithListList(ResourceList):
        schema = BugUserWithListSchema
        data_layer = {'session': session, 'model': BugUser}

    class GroupDetail(ResourceDetail):
        schema = BugGroupSchema
        data_layer = {'session': session, 'model': BugGroup}

    class GroupMembers(ResourceRelationship):
        schema = BugGroupSchema
        data_layer = {'session': session, 'model': BugGroup}

    api = Api(blueprint=Blueprint('api', __name__))
    api.route(UserList, 'bug_user_list', '/users')
    api.route(UserDetail, 'bug_user_detail', '/users/<int:id>')
    api.route(UserWithListList, 'bug_user_with_list_list', '/users_with_list')
    api.route(GroupDetail, 'bug_group_detail', '/groups/<int:id>')
    api.route(GroupMembers, 'bug_group_members', '/groups/<int:id>/relationships/members')
    api.init_app(app)
    return app.test_client()


def send(client, method, path, body):
    return client.open(path, method=method, data=json.dumps(body), content_type='application/vnd.api+json')


def member_names(session, group_id):
    session.expire_all()
    return sorted(user.name for user in session.get(BugGroup, group_id).members)


# numeric ids in relationship requests

@pytest.mark.xfail(strict=True, reason=NUMERIC_IDS)
def test_adding_an_existing_member_by_numeric_id_changes_nothing(client, session):
    # alice (id 1) is already in admins; the id is compared to str ids, so she is "added" again
    response = send(client, 'POST', '/groups/1/relationships/members', {'data': [{'type': 'bug_user', 'id': 1}]})
    assert response.status_code == 204, response.get_data(as_text=True)
    assert member_names(session, 1) == ['alice']


@pytest.mark.xfail(strict=True, reason=NUMERIC_IDS)
def test_removing_a_member_by_numeric_id_removes_it(client, session):
    response = send(client, 'DELETE', '/groups/1/relationships/members', {'data': [{'type': 'bug_user', 'id': 1}]})
    # today the id 1 never matches '1', so nothing is removed and the response is 204 No Content
    assert response.status_code == 200, 'status {}'.format(response.status_code)
    assert member_names(session, 1) == []


# failed writes

@pytest.mark.xfail(strict=True, reason=FAILED_WRITE)
def test_failed_update_is_reported_and_the_session_stays_usable(client, session):
    # the unique violation is flushed while the related group is looked up, outside the try around commit
    body = {'data': {'type': 'bug_user', 'id': '1', 'attributes': {'name': 'bob'},
                     'relationships': {'group': {'data': {'type': 'bug_group', 'id': '2'}}}}}
    response = send(client, 'PATCH', '/users/1', body)
    assert response.status_code == 500
    # today the error escapes the data layer and the formatter copies sqlalchemy's list-valued detail attribute
    assert str(response.json['errors'][0].get('detail')).startswith('Update object error'), response.json

    next_response = client.get('/users', content_type='application/vnd.api+json')
    assert next_response.status_code == 200, next_response.get_data(as_text=True)


@pytest.mark.xfail(strict=True, reason=FAILED_WRITE)
def test_rejected_update_is_not_saved_by_a_later_request(client, session):
    # the nickname is set before the unknown group is looked up, and nothing rolls it back
    body = {'data': {'type': 'bug_user', 'id': '1', 'attributes': {'nickname': 'changed'},
                     'relationships': {'group': {'data': {'type': 'bug_group', 'id': '999'}}}}}
    assert send(client, 'PATCH', '/users/1', body).status_code == 404

    other = {'data': {'type': 'bug_user', 'id': '2', 'attributes': {'nickname': 'bob'}}}
    assert send(client, 'PATCH', '/users/2', other).status_code == 200

    session.expire_all()
    assert session.get(BugUser, 1).nickname == 'alice'


# fields.List

@pytest.mark.xfail(strict=True, reason=LIST_FIELD)
def test_create_on_schema_with_list_field(client):
    body = {'data': {'type': 'bug_user_with_list', 'attributes': {'name': 'carol', 'aliases': ['c']}}}
    response = send(client, 'POST', '/users_with_list', body)
    assert response.status_code == 201, response.get_data(as_text=True)


@pytest.mark.xfail(strict=True, reason=LIST_FIELD)
def test_relationship_filter_on_schema_with_list_field(client):
    filt = [{'name': 'group', 'op': 'has', 'val': {'name': 'name', 'op': 'eq', 'val': 'admins'}}]
    response = client.get('/users_with_list', query_string={'filter': json.dumps(filt)},
                          content_type='application/vnd.api+json')
    assert response.status_code == 200, response.get_data(as_text=True)
    assert [item['attributes']['name'] for item in response.json['data']] == ['alice']
