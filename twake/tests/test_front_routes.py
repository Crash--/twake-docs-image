"""The routes of the Docs front: session, creation with placement, grants."""

import pytest
from rest_framework.test import APIClient

from core import factories, models

from twake import events
from twake.models import Grant
from twake.oidc import WORKPLACE_FQDN_SESSION_KEY

from .conftest import outbox

pytestmark = pytest.mark.django_db

API = "/api/v1.0/twake/"


def test_session_gives_the_id_token_and_the_workplace(alice):
    """The front reads what the bar and the SDK need from the session."""
    client = APIClient()
    client.force_login(alice)
    session = client.session
    session["oidc_id_token"] = "the-id-token"
    session[WORKPLACE_FQDN_SESSION_KEY] = "alice.twake.example"
    session.save()

    response = client.get(f"{API}session/")
    assert response.status_code == 200
    assert response.json() == {
        "sub": "alice",
        "id_token": "the-id-token",
        "workplace_fqdn": "alice.twake.example",
    }
    assert APIClient().get(f"{API}session/").status_code == 401


def test_create_document_with_placement(alice, session_client):
    """A document created from Drive: owner access, and the placement in the
    creation event for the side service to write the pointer there."""
    response = session_client.post(
        f"{API}documents/",
        {
            "title": "From Drive",
            "placement": {"instance": "alice.example", "dir_id": "d1"},
        },
        format="json",
    )
    assert response.status_code == 201, response.json()
    doc = models.Document.objects.get(pk=response.json()["id"])
    assert doc.title == "From Drive"
    assert doc.get_role(alice) == "owner"
    created = outbox(events.DOCUMENT_CREATED)
    assert len(created) == 1
    assert created[0].data["placement"] == {"instance": "alice.example", "dir_id": "d1"}
    assert created[0].actor == "alice"

    response = session_client.post(f"{API}documents/", {}, format="json")
    assert response.status_code == 201
    assert "placement" not in outbox(events.DOCUMENT_CREATED)[-1].data


def test_grants_from_the_modal(alice, bob, session_client):
    """Sharing with a group of the organization: one grant, one event; readers
    see the list, only administrators and owners change it."""
    doc = factories.DocumentFactory(
        users=[(alice, "owner"), (bob, "reader")], link_reach="restricted"
    )
    url = f"{API}documents/{doc.pk}/grants/"

    response = session_client.post(
        url,
        {
            "source_kind": "group",
            "source_id": "g-marketing",
            "name": "Marketing",
            "role": "reader",
        },
        format="json",
    )
    assert response.status_code == 201, response.json()
    grant_id = response.json()["id"]
    assert outbox(events.GRANT_CREATED)[-1].data["source_id"] == "g-marketing"

    # the same group again: the role changes, no second line (story P13 for groups)
    response = session_client.post(
        url,
        {
            "source_kind": "group",
            "source_id": "g-marketing",
            "name": "Marketing",
            "role": "editor",
        },
        format="json",
    )
    assert response.status_code == 200
    assert Grant.objects.filter(document=doc).count() == 1
    assert outbox(events.GRANT_UPDATED)[-1].data["role"] == "editor"

    bob_client = APIClient()
    bob_client.force_login(bob)
    assert bob_client.get(url).status_code == 200
    assert [g["name"] for g in bob_client.get(url).json()] == ["Marketing"]
    assert (
        bob_client.post(
            url,
            {
                "source_kind": "group",
                "source_id": "g2",
                "name": "Sales",
                "role": "reader",
            },
            format="json",
        ).status_code
        == 403
    )
    assert bob_client.delete(f"{url}{grant_id}/").status_code == 403

    # owner is refused, shared drives are not posed from the modal
    assert (
        session_client.post(
            url,
            {"source_kind": "group", "source_id": "g3", "name": "X", "role": "owner"},
            format="json",
        ).status_code
        == 400
    )
    assert (
        session_client.post(
            url,
            {
                "source_kind": "shared_drive",
                "source_id": "d",
                "name": "X",
                "role": "reader",
            },
            format="json",
        ).status_code
        == 400
    )

    assert session_client.delete(f"{url}{grant_id}/").status_code == 204
    assert outbox(events.GRANT_DELETED)[-1].data["grant_id"] == grant_id


def test_share_by_email(alice, bob, session_client, settings):
    """A recipient by email: an access for a known person, an invitation for an
    unknown one, the same person again only changes the role (P1, I1, P13)."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    doc = factories.DocumentFactory(users=[(alice, "owner")], link_reach="restricted")
    url = f"{API}documents/{doc.pk}/accesses/"
    response = session_client.post(
        url, {"email": "BOB@example.com", "role": "reader"}, format="json"
    )
    assert response.status_code == 201, response.json()
    assert response.json()["kind"] == "access"
    assert models.DocumentAccess.objects.get(document=doc, user=bob).role == "reader"
    response = session_client.post(
        url, {"email": "bob@example.com", "role": "editor"}, format="json"
    )
    assert response.status_code == 200
    assert models.DocumentAccess.objects.get(document=doc, user=bob).role == "editor"
    response = session_client.post(
        url, {"email": "carol@example.com", "role": "reader"}, format="json"
    )
    assert response.status_code == 201
    assert response.json()["kind"] == "invitation"
    assert models.Invitation.objects.filter(
        document=doc, email="carol@example.com"
    ).exists()
    from django.core import mail  # noqa: PLC0415

    assert sorted(m.to[0] for m in mail.outbox) == [
        "bob@example.com",
        "carol@example.com",
    ]
    # a reader cannot share
    bob_client = APIClient()
    bob_client.force_login(bob)
    assert (
        bob_client.post(
            url, {"email": "x@example.com", "role": "reader"}, format="json"
        ).status_code
        == 403
    )
