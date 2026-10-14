# -*- coding: utf-8 -*-

import pytest

from flask import Flask


@pytest.fixture()
def app():
    # Flask 3 refuses routes added after an app has handled a request, so every test gets its own app
    app = Flask(__name__)
    return app


@pytest.fixture()
def client(app):
    return app.test_client()
