# -*- coding: utf-8 -*-

"""Known security issues, recorded as strict xfail tests

Each test states the behaviour we want and fails on purpose until the issue in its reason is fixed. strict=True makes
the suite fail once one starts passing, so whoever fixes the issue has to remove its marker.
"""

import json
import logging

import pytest
from flask import Blueprint
from marshmallow_jsonapi import fields
from marshmallow_jsonapi.flask import Schema, Relationship
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from flask_rest_jsonapi import Api, ResourceList, ResourceDetail
from flask_rest_jsonapi.data_layers.alchemy import SqlalchemyDataLayer

OPERATOR_ALLOWLIST = 'filter op can call any column method'
FIELDS_OUTSIDE_SCHEMA = 'filters and sort reach fields outside the schema'
RELATIONSHIP_SCOPE = 'any/has filters bypass loader-criteria scoping'
ERROR_LEAK = 'database errors leak SQL and bound values'
INCLUDE_DEPTH = 'MAX_INCLUDE_DEPTH is never enforced'
MALFORMED_INPUT = 'malformed querystrings and bodies return 500'

Base = declarative_base()


class XfGroup(Base):
    __tablename__ = 'xf_group'

    id = Column(Integer, primary_key=True)
    name = Column(String)
    secret = Column(String)
    members = relationship('XfUser', back_populates='group')


class XfUser(Base):
    __tablename__ = 'xf_user'

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    nickname = Column(String)
    password_hash = Column(String)
    group_id = Column(Integer, ForeignKey('xf_group.id'))
    group = relationship('XfGroup', back_populates='members')


class XfUserSchema(Schema):
    class Meta:
        type_ = 'xf_user'
        self_view = 'api.xf_user_detail'
        self_view_kwargs = {'id': '<id>'}

    id = fields.Integer(as_string=True)
    name = fields.Str(required=True)
    nickname = fields.Str()
    password = fields.Str(load_only=True, attribute='password_hash')
    group = Relationship(related_view='api.xf_group_detail',
                         related_view_kwargs={'id': '<group_id>'},
                         schema='XfGroupSchema',
                         type_='xf_group')


class XfExcludedUserSchema(Schema):
    """Hides a column with Meta.exclude, the way schemas generated from models do"""

    class Meta:
        type_ = 'xf_user_excluded'
        self_view = 'api.xf_user_detail'
        self_view_kwargs = {'id': '<id>'}
        exclude = ('password_hash',)

    id = fields.Integer(as_string=True)
    name = fields.Str(required=True)
    password_hash = fields.Str()


class XfGroupSchema(Schema):
    class Meta:
        type_ = 'xf_group'
        self_view = 'api.xf_group_detail'
        self_view_kwargs = {'id': '<id>'}
        exclude = ('secret',)

    id = fields.Integer(as_string=True)
    name = fields.Str()
    secret = fields.Str()
    members = Relationship(related_view='api.xf_user_list',
                           schema='XfUserSchema',
                           type_='xf_user',
                           many=True)


class ScopedDataLayer(SqlalchemyDataLayer):
    """Hides the admins group from relationship filters, the way a tenant scope would"""

    def related_criteria(self, related_model):
        if related_model is XfGroup:
            return XfGroup.name != 'admins'
        return None


@pytest.fixture(scope='module')
def session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    admins = XfGroup(name='admins', secret='s3cr3t')
    users = XfGroup(name='users', secret='hunter2')
    session.add_all([XfUser(name='alice', nickname='alice', password_hash='hash-a', group=admins),
                     XfUser(name='bob', nickname='bobby', password_hash='hash-b', group=users)])
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def client(app, session):
    class UserList(ResourceList):
        schema = XfUserSchema
        data_layer = {'session': session, 'model': XfUser}

    class UserDetail(ResourceDetail):
        schema = XfUserSchema
        data_layer = {'session': session, 'model': XfUser}

    class ExcludedUserList(ResourceList):
        schema = XfExcludedUserSchema
        data_layer = {'session': session, 'model': XfUser}

    class ScopedUserList(ResourceList):
        schema = XfUserSchema
        data_layer = {'class': ScopedDataLayer, 'session': session, 'model': XfUser}

    class GroupList(ResourceList):
        schema = XfGroupSchema
        data_layer = {'session': session, 'model': XfGroup}

    class GroupDetail(ResourceDetail):
        schema = XfGroupSchema
        data_layer = {'session': session, 'model': XfGroup}

    class Broken(ResourceList):
        schema = XfUserSchema

        def get(self, *args, **kwargs):
            # any error that isn't a JsonApiException: the formatter returns str(e) as the detail
            raise RuntimeError("SELECT password_hash FROM xf_user WHERE name = 'alice'")

    api = Api(blueprint=Blueprint('api', __name__))
    api.route(UserList, 'xf_user_list', '/users')
    api.route(UserDetail, 'xf_user_detail', '/users/<int:id>')
    api.route(ExcludedUserList, 'xf_user_excluded_list', '/excluded_users')
    api.route(ScopedUserList, 'xf_user_scoped_list', '/scoped_users')
    api.route(GroupList, 'xf_group_list', '/groups')
    api.route(GroupDetail, 'xf_group_detail', '/groups/<int:id>')
    api.route(Broken, 'xf_broken', '/broken')
    api.init_app(app)
    return app.test_client()


def get(client, path, **params):
    if 'filter' in params and not isinstance(params['filter'], str):
        params['filter'] = json.dumps(params['filter'])
    return client.get(path, query_string=params, content_type='application/vnd.api+json')


def send(client, method, path, body):
    data = body if isinstance(body, str) else json.dumps(body)
    return client.open(path, method=method, data=data, content_type='application/vnd.api+json')


def names(response):
    return sorted(item['attributes']['name'] for item in response.json['data'])


def assert_no_leak(response):
    body = response.get_data(as_text=True)
    for fragment in ('SELECT', 'INSERT', 'UPDATE ', 'sqlite3', 'xf_user.', 'hash-a', 's3cr3t', 'object at 0x',
                     'function '):
        assert fragment not in body, body


# filter operators

@pytest.mark.xfail(strict=True, reason=OPERATOR_ALLOWLIST)
@pytest.mark.parametrize('op', ['op', 'collate', 'regexp_match', '__eq__', 'startswith', 'match', 'concat',
                                'contains', 'endswith'])
def test_operator_outside_allowlist_is_rejected(client, op):
    response = get(client, '/users', filter=[{'name': 'name', 'op': op, 'val': 'a'}])
    assert response.status_code == 400, response.get_data(as_text=True)


# fields outside the schema

@pytest.mark.xfail(strict=True, reason=FIELDS_OUTSIDE_SCHEMA)
@pytest.mark.parametrize('filt', [
    # `field` is looked up on the model, not the schema: compares users.name to users.password_hash
    {'name': 'name', 'op': 'eq', 'field': 'password_hash'},
    # name__sub passes sub to has() unchecked: equality oracle on any column of the related model
    {'name': 'group__secret', 'op': 'has', 'val': 's3cr3t'},
    # load_only fields are never returned, so they must not be filterable either
    {'name': 'password', 'op': 'like', 'val': 'hash-a%'},
    # Meta.exclude hides the field from responses but leaves it in _declared_fields
    {'name': 'group', 'op': 'has', 'val': {'name': 'secret', 'op': 'eq', 'val': 's3cr3t'}},
])
def test_filter_outside_public_schema_is_rejected(client, filt):
    response = get(client, '/users', filter=[filt])
    assert response.status_code == 400, response.get_data(as_text=True)


@pytest.mark.xfail(strict=True, reason=FIELDS_OUTSIDE_SCHEMA)
@pytest.mark.parametrize('params', [
    {'filter': [{'name': 'password_hash', 'op': 'like', 'val': 'hash-a%'}]},
    {'sort': 'password_hash'},
])
def test_excluded_field_is_rejected(client, params):
    response = get(client, '/excluded_users', **params)
    assert response.status_code == 400, response.get_data(as_text=True)


@pytest.mark.xfail(strict=True, reason=FIELDS_OUTSIDE_SCHEMA)
def test_sort_on_load_only_field_is_rejected(client):
    assert get(client, '/users', sort='password').status_code == 400


# relationship filters and tenant scoping

@pytest.mark.xfail(strict=True, reason=RELATIONSHIP_SCOPE)
@pytest.mark.parametrize('filt', [
    {'name': 'group', 'op': 'has', 'val': {'name': 'name', 'op': 'eq', 'val': 'admins'}},
    {'name': 'group__name', 'op': 'has', 'val': 'admins'},
])
def test_relationship_filter_can_be_scoped(client, filt):
    # with_loader_criteria doesn't reach the EXISTS subquery of any/has, so the data layer needs a way to scope it
    response = get(client, '/scoped_users', filter=[filt])
    assert response.status_code == 200, response.get_data(as_text=True)
    assert names(response) == []


# error details in responses

@pytest.mark.xfail(strict=True, reason=ERROR_LEAK)
def test_write_error_does_not_leak_sql(client, session):
    body = {'data': {'type': 'xf_user', 'attributes': {'name': 'alice', 'password': 'leaked-secret'}}}
    response = send(client, 'POST', '/users', body)
    session.rollback()
    assert response.status_code == 500
    assert_no_leak(response)
    assert 'leaked-secret' not in response.get_data(as_text=True)


@pytest.mark.xfail(strict=True, reason=ERROR_LEAK)
def test_unexpected_error_does_not_leak(client):
    response = get(client, '/broken')
    assert response.status_code == 500
    assert_no_leak(response)


@pytest.mark.xfail(strict=True, reason=ERROR_LEAK)
def test_write_error_log_has_no_bound_values(client, session, caplog):
    body = {'data': {'type': 'xf_user', 'attributes': {'name': 'alice', 'password': 'leaked-secret'}}}
    with caplog.at_level(logging.ERROR):
        send(client, 'POST', '/users', body)
    session.rollback()
    # the failure must be logged for diagnosis, without the values bound into the statement
    assert 'IntegrityError' in caplog.text
    assert 'leaked-secret' not in caplog.text


# include depth

@pytest.mark.xfail(strict=True, reason=INCLUDE_DEPTH)
def test_include_depth_limit_is_enforced(app, client):
    # the check walks the characters of the include string, so only a limit of 1 ever trips (on the '.')
    app.config['MAX_INCLUDE_DEPTH'] = 2
    assert get(client, '/users', include='group.members').status_code == 200
    assert get(client, '/users', include='group.members.group').status_code == 400


# malformed input

@pytest.mark.xfail(strict=True, reason=MALFORMED_INPUT)
@pytest.mark.parametrize('params', [
    {'page[size]': '-1'},
    {'page[number]': '0'},
    {'page[size]': '1,2'},
    {'fields[no_such_type]': 'name'},
    {'filter': '{"name": "name", "op": "eq", "val": "x"}'},
    {'filter': [{'name': 'name', 'op': 'eq', 'val': 10 ** 40}]},
    {'filter': [{'name': 'group', 'op': 'eq', 'val': 1}]},
    {'filter': [{'name': 'group', 'op': 'any', 'val': {'name': 'name', 'op': 'eq', 'val': 'x'}}]},
    {'filter': [{'or': []}]},
    {'filter': '[' + '{"not": ' * 5000 + '{"name": "name", "op": "eq", "val": "x"}' + '}' * 5000 + ']'},
])
def test_malformed_querystring_is_a_bad_request(client, params):
    response = get(client, '/users', **params)
    assert response.status_code == 400, response.get_data(as_text=True)


@pytest.mark.xfail(strict=True, reason=MALFORMED_INPUT)
@pytest.mark.parametrize('method, path, body', [
    ('POST', '/users', '{'),
    ('POST', '/users', '[1]'),
    ('PATCH', '/users/1', {'data': {'type': 'xf_user', 'id': '1', 'attributes': 'x'}}),
    ('POST', '/users', {'data': {'type': 'xf_user', 'attributes': {'name': 'x'}, 'relationships': {'group': 1}}}),
])
def test_malformed_body_is_a_bad_request(client, session, method, path, body):
    response = send(client, method, path, body)
    session.rollback()
    assert 400 <= response.status_code < 500, response.get_data(as_text=True)
