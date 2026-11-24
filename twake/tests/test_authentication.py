"""The on-behalf authentication: the service token plus the user's sub."""

import pytest
from rest_framework.test import APIClient

from core import factories

pytestmark = pytest.mark.django_db

DOCUMENTS = "/api/v1.0/documents/"


def test_on_behalf_acts_as_the_user(alice, bob, on_behalf):
    """The permissions of Docs apply to the named user."""
    doc = factories.DocumentFactory(
        users=[(alice, "owner")], title="A", link_reach="restricted"
    )

    response = on_behalf(alice).get(f"{DOCUMENTS}{doc.pk}/")
    assert response.status_code == 200
    assert response.json()["user_role"] == "owner"

    assert on_behalf(bob).get(f"{DOCUMENTS}{doc.pk}/").status_code == 403

    response = on_behalf(alice).post(
        f"{DOCUMENTS}{doc.pk}/accesses/",
        {"user_id": str(bob.pk), "role": "reader"},
        format="json",
    )
    assert response.status_code == 201
    assert on_behalf(bob).get(f"{DOCUMENTS}{doc.pk}/").json()["user_role"] == "reader"
    assert (
        on_behalf(bob)
        .post(
            f"{DOCUMENTS}{doc.pk}/accesses/",
            {"user_id": str(alice.pk), "role": "editor"},
            format="json",
        )
        .status_code
        == 403
    )


def test_on_behalf_lists_the_entry_points_of_the_user(alice, bob, on_behalf):
    """GET /documents/ as bob lists the highest documents he may read: a
    subpage shared alone is listed as a root."""
    root = factories.DocumentFactory(
        users=[(alice, "owner")], title="A", link_reach="restricted"
    )
    child = factories.DocumentFactory(parent=root, title="A1")
    factories.UserDocumentAccessFactory(document=child, user=bob, role="reader")

    titles = [d["title"] for d in on_behalf(bob).get(DOCUMENTS).json()["results"]]
    assert titles == ["A1"]


def test_on_behalf_refuses_a_bad_token_or_an_unknown_sub(alice):
    """A wrong token or an unknown user get 403, never a session."""
    doc = factories.DocumentFactory(users=[(alice, "owner")], link_reach="restricted")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer wrong", HTTP_X_USER_SUB="alice")
    assert client.get(f"{DOCUMENTS}{doc.pk}/").status_code in (401, 403)
    client.credentials(
        HTTP_AUTHORIZATION="Bearer s2s-test-token", HTTP_X_USER_SUB="nobody"
    )
    assert client.get(f"{DOCUMENTS}{doc.pk}/").status_code in (401, 403)


def test_without_the_header_the_session_applies(alice, session_client):
    """A plain session request still works."""
    doc = factories.DocumentFactory(users=[(alice, "owner")], link_reach="restricted")
    assert session_client.get(f"{DOCUMENTS}{doc.pk}/").status_code == 200
