# -*- coding: utf-8 -*-

"""Compound documents (?include=) against SQLAlchemy 2.0, where joinedload only takes mapped attributes"""

import pytest
from flask import Blueprint
from marshmallow_jsonapi import fields
from marshmallow_jsonapi.flask import Schema, Relationship
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from flask_rest_jsonapi import Api, ResourceList, ResourceDetail

Base = declarative_base()


class IncGroup(Base):
    __tablename__ = 'inc_group'

    id = Column(Integer, primary_key=True)
    name = Column(String)
    members = relationship('IncUser', back_populates='group')


class IncUser(Base):
    __tablename__ = 'inc_user'

    id = Column(Integer, primary_key=True)
    name = Column(String)
    group_id = Column(Integer, ForeignKey('inc_group.id'))
    group = relationship('IncGroup', back_populates='members')


class IncUserSchema(Schema):
    class Meta:
        type_ = 'inc_user'
        self_view = 'api.inc_user_detail'
        self_view_kwargs = {'id': '<id>'}

    id = fields.Integer(as_string=True)
    name = fields.Str()
    group = Relationship(related_view='api.inc_group_detail',
                         related_view_kwargs={'id': '<group_id>'},
                         schema='IncGroupSchema',
                         type_='inc_group')


class IncGroupSchema(Schema):
    class Meta:
        type_ = 'inc_group'
        self_view = 'api.inc_group_detail'
        self_view_kwargs = {'id': '<id>'}

    id = fields.Integer(as_string=True)
    name = fields.Str()
    # attribute= checks that the schema field is mapped to the model attribute before eager loading
    people = Relationship(attribute='members',
                          related_view='api.inc_user_list',
                          schema='IncUserSchema',
                          type_='inc_user',
                          many=True)


@pytest.fixture(scope='module')
def session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    admins = IncGroup(name='admins')
    users = IncGroup(name='users')
    session.add_all([IncUser(name='alice', group=admins), IncUser(name='bob', group=users)])
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def client(app, session):
    class UserList(ResourceList):
        schema = IncUserSchema
        data_layer = {'session': session, 'model': IncUser}

    class UserDetail(ResourceDetail):
        schema = IncUserSchema
        data_layer = {'session': session, 'model': IncUser}

    class GroupList(ResourceList):
        schema = IncGroupSchema
        data_layer = {'session': session, 'model': IncGroup}

    class GroupDetail(ResourceDetail):
        schema = IncGroupSchema
        data_layer = {'session': session, 'model': IncGroup}

    api = Api(blueprint=Blueprint('api', __name__))
    api.route(UserList, 'inc_user_list', '/users')
    api.route(UserDetail, 'inc_user_detail', '/users/<int:id>')
    api.route(GroupList, 'inc_group_list', '/groups')
    api.route(GroupDetail, 'inc_group_detail', '/groups/<int:id>')
    api.init_app(app)
    return app.test_client()


def get(client, path, include):
    return client.get(path, query_string={'include': include}, content_type='application/vnd.api+json')


def included(response):
    return {(item['type'], item['attributes']['name']) for item in response.json['included']}


def test_flat_include(client):
    response = get(client, '/users', 'group')
    assert response.status_code == 200, response.json
    assert included(response) == {('inc_group', 'admins'), ('inc_group', 'users')}


def test_flat_include_through_attribute(client):
    response = get(client, '/groups', 'people')
    assert response.status_code == 200, response.json
    assert included(response) == {('inc_user', 'alice'), ('inc_user', 'bob')}


def test_dotted_include(client):
    response = get(client, '/users', 'group.people')
    assert response.status_code == 200, response.json
    assert {('inc_group', 'admins'), ('inc_user', 'bob')} <= included(response)


def test_dotted_include_on_detail(client):
    response = get(client, '/groups/1', 'people.group')
    assert response.status_code == 200, response.json
    assert included(response) == {('inc_user', 'alice'), ('inc_group', 'admins')}


@pytest.mark.parametrize('include', ['no_such_relationship', 'group.no_such_relationship'])
def test_unknown_include_is_a_bad_request(client, include):
    response = get(client, '/users', include)
    assert response.status_code == 400, response.json
