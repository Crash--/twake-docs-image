"""The routes of the Docs front: session, creation with placement, grants."""

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from rest_framework.test import APIClient

from core import factories, models

from twake import events
from twake.models import Grant, ManagedAccess
from twake.oidc import WORKPLACE_FQDN_SESSION_KEY

from .conftest import outbox

pytestmark = pytest.mark.django_db

API = "/api/v1.0/twake/"


def jwt_expiring_in(seconds):
    """An unsigned JWT whose `exp` is `seconds` from now."""

    def segment(value):
        return (
            base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
        )

    return (
        f"{segment({'alg': 'none'})}.{segment({'exp': int(time.time()) + seconds})}.sig"
    )


def client_with_id_token(user, id_token, workplace="alice.twake.example"):
    """An API client whose session holds the id token of the SSO."""
    client = APIClient()
    client.force_login(user)
    session = client.session
    session["oidc_id_token"] = id_token
    if workplace:
        session[WORKPLACE_FQDN_SESSION_KEY] = workplace
    session.save()
    return client


def test_session_gives_the_id_token_and_the_workplace(alice):
    """The front reads what the bar and the SDK need from the session."""
    id_token = jwt_expiring_in(3600)
    response = client_with_id_token(alice, id_token).get(f"{API}session/")
    assert response.status_code == 200
    assert response.json() == {
        "sub": "alice",
        "id_token": id_token,
        "workplace_fqdn": "alice.twake.example",
    }
    assert APIClient().get(f"{API}session/").status_code == 401


@pytest.mark.parametrize(
    "id_token", [jwt_expiring_in(-60), jwt_expiring_in(4 * 60), "not-a-jwt", None]
)
def test_session_with_an_id_token_about_to_expire(alice, id_token):
    """An id token expired, expiring within 5 minutes or unreadable is not
    given: the front follows a silent login that comes back to the page."""
    response = client_with_id_token(alice, id_token).get(
        f"{API}session/", {"next": "http://docs.example/docs/d1/?x=1"}
    )
    assert response.status_code == 401
    refresh = urlparse(response.json()["refresh_url"])
    assert refresh.path == "/api/v1.0/authenticate/"
    assert parse_qs(refresh.query) == {
        "silent": ["true"],
        "next": ["http://docs.example/docs/d1/?x=1"],
    }


def test_session_off_the_platform_keeps_the_id_token(alice):
    """Without a Workplace there is no bar to sign in: nothing to renew."""
    response = client_with_id_token(alice, "not-a-jwt", workplace=None).get(
        f"{API}session/"
    )
    assert response.status_code == 200
    assert response.json()["workplace_fqdn"] is None


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


def test_managed_access_ids_for_the_dialog(alice, bob, session_client):
    """The dialog hides the accesses a grant posed; one with a direct role stays."""
    doc = factories.DocumentFactory(users=[(alice, "owner")], link_reach="restricted")
    managed = models.DocumentAccess.objects.create(
        document=doc, user=bob, role="reader"
    )
    ManagedAccess.objects.create(access=managed, previous_role="")
    erin = factories.UserFactory(sub="erin", email="erin@example.com")
    direct = models.DocumentAccess.objects.create(
        document=doc, user=erin, role="editor"
    )
    ManagedAccess.objects.create(access=direct, previous_role="editor")

    response = session_client.get(f"{API}documents/{doc.pk}/accesses/managed/")
    assert response.status_code == 200
    assert response.json() == {"access_ids": [str(managed.pk)]}


def test_share_by_email_makes_a_managed_access_direct(alice, bob, session_client):
    """An access a grant posed, given again by hand, is direct from now on: the
    grant going away does not take it back (stories G4, S7)."""
    doc = factories.DocumentFactory(users=[(alice, "owner")], link_reach="restricted")
    access = models.DocumentAccess.objects.create(document=doc, user=bob, role="reader")
    ManagedAccess.objects.create(access=access, previous_role="")

    response = session_client.post(
        f"{API}documents/{doc.pk}/accesses/",
        {"email": "bob@example.com", "role": "editor"},
        format="json",
    )
    assert response.status_code == 200
    assert not ManagedAccess.objects.filter(access=access).exists()
    assert models.DocumentAccess.objects.get(pk=access.pk).role == "editor"


def test_link_reach_authenticated_is_refused(alice, session_client):
    """The reach « every signed-in person » is not offered on the platform (L4):
    the API refuses it, the two others go through."""
    doc = factories.DocumentFactory(users=[(alice, "owner")], link_reach="restricted")
    url = f"/api/v1.0/documents/{doc.pk}/link-configuration/"

    response = session_client.put(url, {"link_reach": "authenticated"}, format="json")
    assert response.status_code == 400
    assert "link_reach" in response.json()
    doc.refresh_from_db()
    assert doc.link_reach == "restricted"

    response = session_client.put(
        url, {"link_reach": "public", "link_role": "reader"}, format="json"
    )
    assert response.status_code == 200
    doc.refresh_from_db()
    assert doc.link_reach == "public"
