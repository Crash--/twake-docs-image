"""Fixtures of the twake app tests."""

from unittest import mock

import pytest
from rest_framework.test import APIClient

from core import factories

from twake.models import Outbox

S2S_TOKEN = "s2s-test-token"


@pytest.fixture(autouse=True)
def mock_reset_service_connections():
    """No test reaches the collaboration server."""
    with mock.patch("core.tasks.access.reset_service_connections_in_cascade.delay"):
        yield


@pytest.fixture(autouse=True)
def mock_indexer():
    """No test reaches the search indexer."""
    with mock.patch("core.tasks.search.trigger_batch_document_indexer"):
        yield


@pytest.fixture(autouse=True)
def s2s_token(settings):
    """The server-to-server token of the tests."""
    settings.SERVER_TO_SERVER_API_TOKENS = [S2S_TOKEN]
    return S2S_TOKEN


@pytest.fixture
def alice():
    """The owner of the documents."""
    return factories.UserFactory(
        sub="alice", email="alice@example.com", language="fr-fr"
    )


@pytest.fixture
def bob():
    """A colleague."""
    return factories.UserFactory(sub="bob", email="bob@example.com", language="en-us")


@pytest.fixture
def session_client(alice):
    """An API client with alice's session."""
    client = APIClient()
    client.force_login(alice)
    return client


@pytest.fixture
def on_behalf():
    """An API client acting on behalf of a user with the service token."""

    def make(user):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {S2S_TOKEN}", HTTP_X_USER_SUB=user.sub
        )
        return client

    return make


@pytest.fixture
def admin_client():
    """An API client with the service token only."""
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {S2S_TOKEN}")
    return client


def outbox(event_type=None):
    """The outbox rows written so far, of one type when given."""
    rows = Outbox.objects.order_by("time", "id")
    if event_type:
        rows = rows.filter(type=event_type)
    return list(rows)
