# -*- coding: utf-8 -*-

"""Known bugs, recorded as strict xfail tests

Each test states the behaviour we want and fails on purpose until the bug in its reason is fixed. strict=True makes
the suite fail once one starts passing, so whoever fixes the bug has to remove its marker.

raises=AssertionError only stops an error raised in the test itself (a fixture or helper failing, a KeyError on a
changed response shape) from counting as the expected failure. The library turns a server-side exception into a 500
response, which fails a status assertion just as the bug does, so the control tests at the top must exercise every
route and method the xfails use.
"""

import functools
import json

import pytest
from flask import Blueprint
from marshmallow import Schema as MarshmallowSchema
from marshmallow_jsonapi import fields
from marshmallow_jsonapi.flask import Schema, Relationship
from sqlalchemy import create_engine, event, Column, Integer, String, ForeignKey, JSON
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from flask_rest_jsonapi import Api, ResourceList, ResourceDetail, ResourceRelationship
from flask_rest_jsonapi.data_layers.alchemy import SqlalchemyDataLayer
from flask_rest_jsonapi.schema import get_nested_fields

known_bug = functools.partial(pytest.mark.xfail, strict=True, raises=AssertionError)

NUMERIC_IDS = 'relationship POST/DELETE compare numeric ids to str ids (POST re-appends, DELETE removes nothing)'
FAILED_WRITE = 'failed writes leave the session broken or half-applied'
ERROR_DETAIL = "the error formatter copies an exception's non-str detail attribute"
PARTIAL_RELATIONSHIP_WRITE = 'relationship writes leave partial changes on error'
DUPLICATE_REMOVAL = 'relationship DELETE with the same id twice returns 500 (list.remove of an already removed member)'
LIST_FIELD = 'get_nested_fields reads List.container, which marshmallow 3 does not have'
NON_RELATIONSHIP_INCLUDE = 'include of a declared non-relationship field returns 500'
CLASS_SCHEMA_INCLUDE = 'dotted include through a relationship declared with a schema class returns 500'
SHARED_DATA_LAYER = "subclasses share their parent's data layer"
MUTATED_SCHEMA_KWARGS = 'get_schema_kwargs is mutated by requests'
CONFIG_ERROR_RESOURCE = 'the data_layer-not-a-dict error names the metaclass, not the resource'
MISSING_SESSION_ERROR = 'the missing-session error raises AttributeError (the data layer has no resource yet)'
EAGERLOAD_FLAG = 'eagerload_includes: True in data_layer shadows the method and every GET returns 500'
SHARED_DATA_LAYER_DICT = "creating a data layer pops 'class' and 'methods' from a shared data_layer dict"
RELATIONSHIP_INT_IDS = 'relationship endpoints return integer ids instead of strings'

# a duplicate unique name together with a relationship, so the violation is flushed while the update is applied
DUPLICATE_NAME = {'data': {'type': 'bug_user', 'id': '1', 'attributes': {'name': 'bob'},
                           'relationships': {'group': {'data': {'type': 'bug_group', 'id': '2'}}}}}

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


class BugTagSchema(MarshmallowSchema):
    label = fields.Str()


class BugUserWithTagsSchema(BugUserSchema):
    class Meta:
        type_ = 'bug_user_with_tags'
        self_view = 'api.bug_user_detail'
        self_view_kwargs = {'id': '<id>'}

    tags = fields.List(fields.Nested(BugTagSchema))


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


class BugUserWithSchemaClassSchema(Schema):
    """Declares its relationship with a schema class instead of a class name"""

    class Meta:
        type_ = 'bug_user_with_schema_class'
        self_view = 'api.bug_user_detail'
        self_view_kwargs = {'id': '<id>'}

    id = fields.Integer(as_string=True)
    name = fields.Str(required=True)
    group = Relationship(related_view='api.bug_group_detail',
                         related_view_kwargs={'id': '<group_id>'},
                         schema=BugGroupSchema,
                         type_='bug_group')


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

    class UserWithSchemaClassList(ResourceList):
        schema = BugUserWithSchemaClassSchema
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
    api.route(UserWithSchemaClassList, 'bug_user_with_schema_class_list', '/users_with_schema_class')
    api.route(GroupDetail, 'bug_group_detail', '/groups/<int:id>')
    api.route(GroupMembers, 'bug_group_members', '/groups/<int:id>/relationships/members')
    api.init_app(app)
    return app.test_client()


def send(client, method, path, body):
    return client.open(path, method=method, data=json.dumps(body), content_type='application/vnd.api+json')


def get(client, path, **params):
    return client.get(path, query_string=params, content_type='application/vnd.api+json')


def member_names(session, group_id):
    session.expire_all()
    return sorted(user.name for user in session.get(BugGroup, group_id).members)


def only_bob(self, view_kwargs):
    return self.session.query(BugUser).filter_by(name='bob')


def count_statements(session, request):
    """Run request() and return the SQL statements it executed"""
    session.expunge_all()
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(session.get_bind(), 'before_cursor_execute', record)
    try:
        response = request()
    finally:
        event.remove(session.get_bind(), 'before_cursor_execute', record)
    return response, statements


def test_fixture_endpoints_work(client):
    # control: if the fixtures break, the xfails below would fail for the wrong reason and still pass as expected
    assert get(client, '/users').status_code == 200
    response = get(client, '/groups/1/relationships/members')
    assert response.status_code == 200, response.get_data(as_text=True)
    assert [(item['type'], str(item['id'])) for item in response.json['data']] == [('bug_user', '1')]


def test_fixture_write_endpoints_work(client, session):
    # control for the other routes and methods the xfails below use
    for path, params in (('/users/1', {}), ('/users_with_list', {}), ('/users_with_schema_class', {'include': 'group'})):
        response = get(client, path, **params)
        assert response.status_code == 200, response.get_data(as_text=True)

    body = {'data': {'type': 'bug_user', 'id': '2', 'attributes': {'nickname': 'bob'}}}
    response = send(client, 'PATCH', '/users/2', body)
    assert response.status_code == 200, response.get_data(as_text=True)

    members = {'data': [{'type': 'bug_user', 'id': '2'}]}
    response = send(client, 'POST', '/groups/1/relationships/members', members)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert member_names(session, 1) == ['alice', 'bob']
    response = send(client, 'DELETE', '/groups/1/relationships/members', members)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert member_names(session, 1) == ['alice']


# numeric ids in relationship requests

@known_bug(reason=NUMERIC_IDS)
def test_adding_an_existing_member_by_numeric_id_changes_nothing(client, session):
    # alice (id 1) is already in admins; the id is compared to str ids, so she is "added" again
    response = send(client, 'POST', '/groups/1/relationships/members', {'data': [{'type': 'bug_user', 'id': 1}]})
    assert response.status_code == 204, response.get_data(as_text=True)


@known_bug(reason=NUMERIC_IDS)
def test_removing_a_member_by_numeric_id_removes_it(client, session):
    response = send(client, 'DELETE', '/groups/1/relationships/members', {'data': [{'type': 'bug_user', 'id': 1}]})
    # today the id 1 never matches '1', so nothing is removed and the response is 204 No Content
    assert response.status_code == 200, 'status {}'.format(response.status_code)
    assert member_names(session, 1) == []


# failed writes

@known_bug(reason=ERROR_DETAIL)
def test_failed_update_reports_a_string_detail(client, session):
    response = send(client, 'PATCH', '/users/1', DUPLICATE_NAME)
    assert response.status_code in (409, 500)
    # today the error escapes the data layer and the formatter copies sqlalchemy's list-valued detail attribute
    detail = response.json['errors'][0].get('detail')
    assert isinstance(detail, str) and not detail.startswith('['), response.json


@known_bug(reason=FAILED_WRITE)
def test_session_is_usable_after_a_failed_update(client, session):
    # the unique violation is flushed while the update is applied, outside the try around commit
    assert send(client, 'PATCH', '/users/1', DUPLICATE_NAME).status_code in (409, 500)
    response = get(client, '/users')
    assert response.status_code == 200, response.get_data(as_text=True)


@known_bug(reason=FAILED_WRITE)
def test_rejected_update_is_not_saved_by_a_later_request(client, session):
    # the nickname is set before the unknown group is looked up, and nothing rolls it back
    body = {'data': {'type': 'bug_user', 'id': '1', 'attributes': {'nickname': 'changed'},
                     'relationships': {'group': {'data': {'type': 'bug_group', 'id': '999'}}}}}
    assert send(client, 'PATCH', '/users/1', body).status_code == 404

    other = {'data': {'type': 'bug_user', 'id': '2', 'attributes': {'nickname': 'bob'}}}
    assert send(client, 'PATCH', '/users/2', other).status_code == 200

    session.expire_all()
    assert session.get(BugUser, 1).nickname == 'alice'


@known_bug(reason=DUPLICATE_REMOVAL)
def test_removing_the_same_member_twice_is_not_an_error(client, session):
    # the ids to remove are computed once, so the second '1' removes alice again and list.remove raises ValueError
    body = {'data': [{'type': 'bug_user', 'id': '1'}, {'type': 'bug_user', 'id': '1'}]}
    response = send(client, 'DELETE', '/groups/1/relationships/members', body)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert member_names(session, 1) == []


@known_bug(reason=PARTIAL_RELATIONSHIP_WRITE)
def test_rejected_member_add_is_not_saved_by_a_later_request(client, session):
    # bob is added to admins before the unknown id 999 is looked up, and nothing rolls it back
    body = {'data': [{'type': 'bug_user', 'id': '2'}, {'type': 'bug_user', 'id': '999'}]}
    assert send(client, 'POST', '/groups/1/relationships/members', body).status_code == 404

    other = {'data': {'type': 'bug_user', 'id': '1', 'attributes': {'nickname': 'al'}}}
    assert send(client, 'PATCH', '/users/1', other).status_code == 200

    session.expire_all()
    assert session.get(BugUser, 2).group_id == 2


# fields.List

@known_bug(reason=LIST_FIELD)
def test_create_on_schema_with_list_field(client):
    body = {'data': {'type': 'bug_user_with_list', 'attributes': {'name': 'carol', 'aliases': ['c']}}}
    response = send(client, 'POST', '/users_with_list', body)
    assert response.status_code == 201, response.get_data(as_text=True)


@known_bug(reason=LIST_FIELD)
def test_relationship_filter_on_schema_with_list_field(client):
    filt = [{'name': 'group', 'op': 'has', 'val': {'name': 'name', 'op': 'eq', 'val': 'admins'}}]
    response = get(client, '/users_with_list', filter=json.dumps(filt))
    assert response.status_code == 200, response.get_data(as_text=True)
    assert [item['attributes']['name'] for item in response.json['data']] == ['alice']


@known_bug(reason=LIST_FIELD)
def test_list_of_nested_is_a_nested_field():
    # a fix that only silences the AttributeError (e.g. getattr(value, 'container', None)) must not pass
    try:
        nested_fields = get_nested_fields(BugUserWithTagsSchema)
        list_fields = get_nested_fields(BugUserWithListSchema)
    except AttributeError:
        nested_fields = list_fields = ['aliases']
    assert 'tags' in nested_fields
    # and a fix that treats every List as nested must not pass either
    assert 'aliases' not in list_fields


# includes

@known_bug(reason=NON_RELATIONSHIP_INCLUDE)
@pytest.mark.parametrize('path', ['/users', '/users/1'])
@pytest.mark.parametrize('include', ['name', 'name.group', 'group.name'])
def test_include_of_non_relationship_field_is_a_bad_request(client, path, include):
    response = get(client, path, include=include)
    assert response.status_code == 400, response.get_data(as_text=True)


@known_bug(reason=CLASS_SCHEMA_INCLUDE)
def test_dotted_include_through_schema_class(client, session):
    response, statements = count_statements(
        session, lambda: get(client, '/users_with_schema_class', include='group.members'))
    assert response.status_code == 200, response.get_data(as_text=True)
    included = {(item['type'], item['attributes']['name']) for item in response.json['included']}
    assert {('bug_group', 'admins'), ('bug_user', 'alice')} <= included
    # one count query and one joined select; a fix that skips eager loading would add queries per row
    assert len(statements) == 2, statements


# resource classes

@known_bug(reason=SHARED_DATA_LAYER)
def test_subclass_gets_its_own_data_layer(app, client, session):
    class ParentList(ResourceList):
        schema = BugUserSchema
        data_layer = {'session': session, 'model': BugUser, 'methods': {'query': only_bob}}

    class ChildList(ParentList):
        schema = BugUserWithListSchema

    # every request sets _data_layer.resource, so concurrent parent and child requests can use each other's schema
    child = ChildList._data_layer
    assert child is not ParentList._data_layer
    # the child's own layer must keep the parent's class, model, session and method overrides
    assert type(child) is type(ParentList._data_layer)
    assert child.model is BugUser and child.session is session
    Api(app).route(ChildList, 'bug_child_list', '/child_users')
    response = get(client, '/child_users')
    assert response.status_code == 200, response.get_data(as_text=True)
    assert [(item['type'], item['attributes']['name']) for item in response.json['data']] == \
        [('bug_user_with_list', 'bob')]


@known_bug(reason=MUTATED_SCHEMA_KWARGS)
def test_requests_do_not_change_get_schema_kwargs(app, client, session):
    class KwargsUserList(ResourceList):
        schema = BugUserSchema
        data_layer = {'session': session, 'model': BugUser}
        get_schema_kwargs = {'only': ('name', 'group')}

    class KwargsUserDetail(ResourceDetail):
        schema = BugUserSchema
        data_layer = {'session': session, 'model': BugUser}
        get_schema_kwargs = {'only': ('name', 'group')}

    api = Api(app)
    api.route(KwargsUserList, 'kwargs_user_list', '/kwargs_users')
    api.route(KwargsUserDetail, 'kwargs_user_detail', '/kwargs_users/<int:id>')
    assert get(client, '/kwargs_users', include='group').status_code == 200
    assert get(client, '/kwargs_users/1', include='group').status_code == 200
    # concurrent requests share these dicts, so one request's include_data can leak into another's response
    assert KwargsUserList.get_schema_kwargs == {'only': ('name', 'group')}
    assert KwargsUserDetail.get_schema_kwargs == {'only': ('name', 'group')}


@known_bug(reason=CONFIG_ERROR_RESOURCE)
def test_data_layer_not_a_dict_error_names_the_resource():
    with pytest.raises(Exception, match='as dict in NotADictDataLayer$'):
        class NotADictDataLayer(ResourceList):
            data_layer = ['not', 'a', 'dict']


@known_bug(reason=MISSING_SESSION_ERROR)
def test_missing_session_error_names_the_resource():
    with pytest.raises(Exception, match='You must provide a session .* in MissingSession$'):
        class MissingSession(ResourceList):
            data_layer = {'model': BugUser}


# data layer configuration

@known_bug(reason=EAGERLOAD_FLAG)
def test_eagerload_includes_flag_set_to_true(app, client, session):
    class FlagUserList(ResourceList):
        schema = BugUserSchema
        data_layer = {'session': session, 'model': BugUser, 'eagerload_includes': True}

    Api(app).route(FlagUserList, 'bug_flag_user_list', '/flag_users')
    response = get(client, '/flag_users', include='group')
    assert response.status_code == 200, response.get_data(as_text=True)


@known_bug(reason=SHARED_DATA_LAYER_DICT)
def test_resources_can_share_a_data_layer_dict(session):
    class CustomDataLayer(SqlalchemyDataLayer):
        pass

    shared = {'class': CustomDataLayer, 'session': session, 'model': BugUser, 'methods': {'query': only_bob}}

    class FirstList(ResourceList):
        schema = BugUserSchema
        data_layer = shared

    class SecondList(ResourceList):
        schema = BugUserSchema
        data_layer = shared

    # the second resource silently gets a plain SqlalchemyDataLayer without the custom query
    assert set(shared) >= {'class', 'methods'}, shared
    assert type(SecondList._data_layer) is CustomDataLayer


# relationship endpoints

@known_bug(reason=RELATIONSHIP_INT_IDS)
def test_relationship_identifiers_have_string_ids(client):
    response = get(client, '/groups/1/relationships/members')
    assert response.status_code == 200, response.get_data(as_text=True)
    assert all(isinstance(item['id'], str) for item in response.json['data']), response.json['data']
