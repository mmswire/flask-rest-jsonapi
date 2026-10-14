# -*- coding: utf-8 -*-

"""Known security issues, recorded as strict xfail tests

Each test states the behaviour we want and fails on purpose until the issue in its reason is fixed. strict=True makes
the suite fail once one starts passing, so whoever fixes the issue has to remove its marker. Parametrized cases with
different causes carry their own marker, so fixing one cause flips only its cases.

raises=AssertionError only stops an error raised in the test itself (a fixture or helper failing, a KeyError on a
changed response shape) from counting as the expected failure. The library turns a server-side exception into a 500
response, which fails a status assertion just as the issue does, so the control test at the top must exercise every
route, method and query feature the xfails use.
"""

import functools
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

known_issue = functools.partial(pytest.mark.xfail, strict=True, raises=AssertionError)


def case(*values, reason, id):
    return pytest.param(*values, marks=known_issue(reason=reason), id=id)


OPERATOR_ALLOWLIST = 'filter op can call any column method'
RELATIONSHIP_SCOPE = 'any/has filters bypass loader-criteria scoping'
ERROR_LEAK = 'database write errors leak SQL and bound values'
UNEXPECTED_ERROR_LEAK = 'unexpected errors return str(e) as the error detail'
NO_ERROR_LOG = 'write failures are not logged'
INCLUDE_DEPTH = 'MAX_INCLUDE_DEPTH of 2 or more is never enforced'
LOST_DECORATORS = "subclasses lose their parent resource's decorators (e.g. authentication)"
API_DECORATORS = 'Api(decorators=...) is never applied to routes'
PAGINATION_BOUNDS = 'negative or zero page[size] passes the MAX_PAGE_SIZE check and returns every row'

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
    """Declares a field but hides it with Meta.exclude; marshmallow keeps it in _declared_fields"""

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


def test_fixture_endpoints_work(client):
    # control: if the fixtures break, the xfails below would fail for the wrong reason and still pass as expected
    assert names(get(client, '/users')) == ['alice', 'bob']
    assert names(get(client, '/groups')) == ['admins', 'users']
    for path in ('/excluded_users', '/scoped_users', '/users/1'):
        response = get(client, path)
        assert response.status_code == 200, response.get_data(as_text=True)

    # the filter, sort, include and write paths the xfails below rely on
    assert names(get(client, '/users', filter=[{'name': 'name', 'op': 'eq', 'val': 'alice'}])) == ['alice']
    assert names(get(client, '/users', filter=[{'name': 'name', 'op': 'like', 'val': 'b%'}])) == ['bob']
    scoped = get(client, '/scoped_users',
                 filter=[{'name': 'group', 'op': 'has', 'val': {'name': 'name', 'op': 'eq', 'val': 'users'}}])
    assert names(scoped) == ['bob']
    assert [item['attributes']['name'] for item in get(client, '/users', sort='-name').json['data']] == ['bob', 'alice']
    assert get(client, '/users', include='group.members').status_code == 200

    patch = {'data': {'type': 'xf_user', 'id': '2', 'attributes': {'nickname': 'bobby'}}}
    assert send(client, 'PATCH', '/users/2', patch).status_code == 200
    # create and delete again, so the module-scoped database is left as it was
    created = send(client, 'POST', '/users', {'data': {'type': 'xf_user', 'attributes': {'name': 'carol'}}})
    assert created.status_code == 201, created.get_data(as_text=True)
    assert send(client, 'DELETE', '/users/' + created.json['data']['id'], {}).status_code == 200


# filter operators

@known_issue(reason=OPERATOR_ALLOWLIST)
@pytest.mark.parametrize('op', ['op', 'collate', 'regexp_match', '__eq__', 'startswith', 'match', 'concat',
                                'contains', 'endswith'])
def test_operator_outside_allowlist_is_rejected(client, op):
    response = get(client, '/users', filter=[{'name': 'name', 'op': op, 'val': 'a'}])
    assert response.status_code == 400, response.get_data(as_text=True)


# fields outside the schema

@pytest.mark.parametrize('filt', [
    case({'name': 'name', 'op': 'eq', 'field': 'password_hash'},
         reason='filter field is looked up on the model, not the schema', id='field-on-model'),
    case({'name': 'group__secret', 'op': 'has', 'val': 's3cr3t'},
         reason='fields hidden with Meta.exclude can be filtered', id='related-subfield-excluded'),
    case({'name': 'password', 'op': 'like', 'val': 'hash-a%'},
         reason='load_only fields can be filtered', id='load-only'),
    case({'name': 'group', 'op': 'has', 'val': {'name': 'secret', 'op': 'eq', 'val': 's3cr3t'}},
         reason='fields hidden with Meta.exclude can be filtered', id='related-excluded'),
    # passes today only because the model has no `password` attribute; mapping `field` through the schema must not
    # open the load_only column it maps to, so this case has no marker
    pytest.param({'name': 'name', 'op': 'lt', 'field': 'password'}, id='field-load-only'),
])
def test_filter_outside_public_schema_is_rejected(client, filt):
    response = get(client, '/users', filter=[filt])
    assert response.status_code == 400, response.get_data(as_text=True)


@known_issue(reason='name__x passes x to any()/has() without a schema check')
def test_related_subfield_outside_schema_is_rejected(client):
    # password_hash is a model column XfUserSchema never declares (it only exposes it as the load_only `password`)
    response = get(client, '/groups', filter=[{'name': 'members__password_hash', 'op': 'any', 'val': 'hash-a'}])
    assert response.status_code == 400, response.get_data(as_text=True)


@pytest.mark.parametrize('params', [
    case({'filter': [{'name': 'password_hash', 'op': 'like', 'val': 'hash-a%'}]},
         reason='fields hidden with Meta.exclude can be filtered', id='filter'),
    case({'sort': 'password_hash'},
         reason='fields hidden with Meta.exclude can be sorted', id='sort'),
])
def test_excluded_field_is_rejected(client, params):
    response = get(client, '/excluded_users', **params)
    assert response.status_code == 400, response.get_data(as_text=True)


@known_issue(reason='load_only fields can be sorted')
def test_sort_on_load_only_field_is_rejected(client):
    assert get(client, '/users', sort='password').status_code == 400


# relationship filters and tenant scoping

@known_issue(reason=RELATIONSHIP_SCOPE)
@pytest.mark.parametrize('make_filter', [
    lambda group: {'name': 'group', 'op': 'has', 'val': {'name': 'name', 'op': 'eq', 'val': group}},
    lambda group: {'name': 'group__name', 'op': 'has', 'val': group},
], ids=['nested', 'subfield'])
def test_relationship_filter_can_be_scoped(client, make_filter):
    # with_loader_criteria doesn't reach the EXISTS subquery of any/has, so the data layer needs a way to scope it
    visible = get(client, '/scoped_users', filter=[make_filter('users')])
    assert names(visible) == ['bob'], visible.get_data(as_text=True)
    response = get(client, '/scoped_users', filter=[make_filter('admins')])
    assert response.status_code == 200, response.get_data(as_text=True)
    assert names(response) == []


# error details in responses and logs

@known_issue(reason=ERROR_LEAK)
def test_write_error_does_not_leak_sql(client, session):
    body = {'data': {'type': 'xf_user', 'attributes': {'name': 'alice', 'password': 'leaked-secret'}}}
    response = send(client, 'POST', '/users', body)
    session.rollback()
    assert response.status_code in (409, 500)
    assert_no_leak(response)
    assert 'leaked-secret' not in response.get_data(as_text=True)


@known_issue(reason=UNEXPECTED_ERROR_LEAK)
def test_unexpected_error_does_not_leak(client):
    response = get(client, '/broken')
    assert response.status_code == 500
    assert_no_leak(response)


def test_write_error_log_has_no_bound_values(client, session, caplog):
    # passes today; guards against a fix that logs the failed statement together with its bound values
    body = {'data': {'type': 'xf_user', 'attributes': {'name': 'alice', 'password': 'leaked-secret'}}}
    with caplog.at_level(logging.DEBUG):
        response = send(client, 'POST', '/users', body)
    session.rollback()
    assert response.status_code in (409, 500), response.get_data(as_text=True)
    assert 'leaked-secret' not in caplog.text


@known_issue(reason=NO_ERROR_LOG)
def test_write_error_is_logged(client, session, caplog):
    body = {'data': {'type': 'xf_user', 'attributes': {'name': 'alice', 'password': 'leaked-secret'}}}
    with caplog.at_level(logging.WARNING):
        response = send(client, 'POST', '/users', body)
    session.rollback()
    assert response.status_code in (409, 500), response.get_data(as_text=True)
    assert 'IntegrityError' in caplog.text


# include depth

@known_issue(reason=INCLUDE_DEPTH)
def test_include_depth_limit_is_enforced(app, client):
    # the check walks the characters of the include string, so only limits 0 and 1 ever trip
    app.config['MAX_INCLUDE_DEPTH'] = 2
    assert get(client, '/users', include='group.members').status_code == 200
    # each path is within the limit; a check that counts dots across the whole string would reject this
    assert get(client, '/users', include='group.members,group.members').status_code == 200
    assert get(client, '/users', include='group.members.group').status_code == 400


# resource classes

@known_issue(reason=LOST_DECORATORS)
def test_subclass_keeps_parent_decorators(app, client, session):
    calls = []

    def deco(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            calls.append(view.__name__)
            return view(*args, **kwargs)
        return wrapper

    class AuthedList(ResourceList):
        decorators = (deco,)

    class SubUserList(AuthedList):
        schema = XfUserSchema
        data_layer = {'session': session, 'model': XfUser}

    Api(app).route(SubUserList, 'xf_sub_user_list', '/sub_users')
    assert get(client, '/sub_users').status_code == 200
    assert calls == ['xf_sub_user_list']


@known_issue(reason=API_DECORATORS)
def test_api_decorators_are_applied(app, client, session):
    calls = []

    def deco(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            calls.append(view.__name__)
            return view(*args, **kwargs)
        return wrapper

    class DecoratedUserList(ResourceList):
        schema = XfUserSchema
        data_layer = {'session': session, 'model': XfUser}

    # documented as the way to put e.g. login_required on every endpoint
    Api(app, decorators=(deco,)).route(DecoratedUserList, 'xf_decorated_user_list', '/decorated_users')
    assert get(client, '/decorated_users').status_code == 200
    assert calls == ['xf_decorated_user_list']


# pagination bounds

@known_issue(reason=PAGINATION_BOUNDS)
@pytest.mark.parametrize('size', ['-1', '0'])
def test_page_size_respects_max_page_size(app, client, size):
    app.config['MAX_PAGE_SIZE'] = 1
    # -1 becomes LIMIT -1 and 0 disables pagination; both pass the `> MAX_PAGE_SIZE` check and return every row
    response = get(client, '/users', **{'page[size]': size})
    assert response.status_code == 400, response.get_data(as_text=True)


# malformed input

@pytest.mark.parametrize('params', [
    case({'page[size]': '-1'}, reason='a negative page[size] becomes LIMIT -1 and returns every row',
         id='negative-page-size'),
    case({'page[number]': '0'}, reason='page[number] below 1 becomes a negative OFFSET instead of a 400',
         id='zero-page-number'),
    case({'page[size]': '1,2'}, reason='page values that are lists crash with 500', id='list-page-size'),
    case({'fields[no_such_type]': 'name'}, reason='unknown fields[type] crashes with 500', id='unknown-type'),
    case({'filter': '{"name": "name", "op": "eq", "val": "x"}'},
         reason='a filter that is not a list crashes with 500', id='filter-not-list'),
    case({'filter': [{'name': 'name', 'op': 'eq', 'val': 10 ** 40}]},
         reason='filter values the database cannot bind crash with 500', id='int-overflow'),
    case({'filter': [{'name': 'group', 'op': 'eq', 'val': 1}]},
         reason='comparing a relationship with eq crashes with 500', id='relationship-eq'),
    case({'filter': [{'name': 'group', 'op': 'any', 'val': {'name': 'name', 'op': 'eq', 'val': 'x'}}]},
         reason='any on a to-one relationship crashes with 500', id='any-on-to-one'),
    case({'filter': [{'or': []}]}, reason='an empty or/and filter crashes with 500', id='empty-or'),
    case({'filter': '[' + '{"not": ' * 5000 + '{"name": "name", "op": "eq", "val": "x"}' + '}' * 5000 + ']'},
         reason='deeply nested filters crash with 500 (RecursionError)', id='deep-nesting'),
])
def test_malformed_querystring_is_a_bad_request(client, params):
    response = get(client, '/users', **params)
    assert response.status_code == 400, response.get_data(as_text=True)


@pytest.mark.parametrize('method, path, body', [
    case('POST', '/users', '{', reason='invalid JSON bodies return 500', id='invalid-json'),
    case('POST', '/users', '[1]', reason='non-object JSON bodies return 500', id='non-object-body'),
    case('PATCH', '/users/1', {'data': {'type': 'xf_user', 'id': '1', 'attributes': 'x'}},
         reason='non-object attributes return 500', id='non-object-attributes'),
    case('POST', '/users', {'data': {'type': 'xf_user', 'attributes': {'name': 'x'}, 'relationships': {'group': 1}}},
         reason='non-object relationship values return 500', id='non-object-relationship'),
])
def test_malformed_body_is_a_bad_request(client, session, method, path, body):
    response = send(client, method, path, body)
    session.rollback()
    assert 400 <= response.status_code < 500, response.get_data(as_text=True)
