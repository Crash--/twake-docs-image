"""The admin routes of the side service."""

from django.core import mail

import pytest

from core import factories, models

from twake import events
from twake.models import Grant, ManagedAccess

from .conftest import outbox

pytestmark = pytest.mark.django_db


def test_admin_routes_need_the_service_token(session_client):
    """A session is not enough for an admin route."""
    response = session_client.post(
        "/twake/users/", {"sub": "x", "email": "x@example.com"}, format="json"
    )
    assert response.status_code in (401, 403)


def test_ensure_user_creates_and_converts_invitations(alice, admin_client):
    """A user created ahead of login gets the accesses of their invitations,
    which never expire silently (story I2)."""
    doc = factories.DocumentFactory(
        users=[(alice, "owner")], title="A", link_reach="restricted"
    )
    models.Invitation.objects.create(
        document=doc, email="carol@example.com", role="editor", issuer=alice
    )

    response = admin_client.post(
        "/twake/users/",
        {"sub": "carol", "email": "carol@example.com", "full_name": "Carol"},
        format="json",
    )
    assert response.status_code == 201
    assert response.json()["created"] is True
    carol = models.User.objects.get(sub="carol")
    assert carol.email == "carol@example.com"
    access = models.DocumentAccess.objects.get(document=doc, user=carol)
    assert access.role == "editor"
    assert not models.Invitation.objects.filter(document=doc).exists()
    assert outbox(events.ACCESS_CREATED)[-1].data["sub"] == "carol"

    # Idempotent
    response = admin_client.post(
        "/twake/users/", {"sub": "carol", "email": "carol@example.com"}, format="json"
    )
    assert response.status_code == 200
    assert response.json()["created"] is False
    assert models.User.objects.filter(sub="carol").count() == 1


def test_ensure_user_refuses_another_user_email(alice, admin_client):
    """An email already bound to another sub is a conflict."""
    response = admin_client.post(
        "/twake/users/", {"sub": "alice2", "email": alice.email}, format="json"
    )
    assert response.status_code == 409


def test_user_settings(alice, admin_client):
    """Language and timezone of the Workplace settings land on the user."""
    response = admin_client.put(
        "/twake/users/settings/",
        {"email": alice.email, "language": "en-us", "timezone": "Europe/Berlin"},
        format="json",
    )
    assert response.status_code == 200
    alice.refresh_from_db()
    assert alice.language == "en-us"
    assert str(alice.timezone) == "Europe/Berlin"

    response = admin_client.put(
        "/twake/users/settings/",
        {"email": "nobody@example.com", "language": "en-us"},
        format="json",
    )
    assert response.status_code == 404


def test_managed_accesses_pose_and_remove_accesses(alice, bob, admin_client, settings):
    """
    The accesses a grant gives: created with the sharing email in the
    recipient's language, the strongest kept over a direct access, removed or
    given back when the grant stops (stories G1, G4, G5, G7, S5, S7).
    """
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    doc = factories.DocumentFactory(
        users=[(alice, "owner")], title="A", link_reach="restricted"
    )
    erin = factories.UserFactory(sub="erin", email="erin@example.com", language="de-de")
    # bob already has a direct editor access
    factories.UserDocumentAccessFactory(document=doc, user=bob, role="editor")
    mail.outbox.clear()
    url = f"/twake/documents/{doc.pk}/managed-accesses/"

    response = admin_client.put(
        url,
        {
            "accesses": [
                {"email": "erin@example.com", "role": "reader"},
                {"email": "bob@example.com", "role": "reader"},
                {"email": "carol@example.com", "role": "reader"},
            ]
        },
        format="json",
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["created"] == ["erin@example.com"]
    assert body["invited"] == ["carol@example.com"]
    assert models.DocumentAccess.objects.get(document=doc, user=erin).role == "reader"
    # bob keeps the strongest
    assert models.DocumentAccess.objects.get(document=doc, user=bob).role == "editor"
    assert ManagedAccess.objects.get(access__user=bob).previous_role == "editor"
    assert models.Invitation.objects.filter(
        document=doc, email="carol@example.com"
    ).exists()
    # one sharing email per created access or invitation, erin's in German
    recipients = sorted(m.to[0] for m in mail.outbox)
    assert recipients == ["carol@example.com", "erin@example.com"]
    erin_mail = next(m for m in mail.outbox if m.to == ["erin@example.com"])
    assert "Dokument" in erin_mail.subject or "Dokument" in erin_mail.body
    assert outbox(events.ACCESS_CREATED)[-1].data["managed"] is True

    # the grant now gives editor to erin, and nothing to bob anymore
    mail.outbox.clear()
    response = admin_client.put(
        url,
        {"accesses": [{"email": "erin@example.com", "role": "editor"}]},
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["updated"] == ["erin@example.com"]
    assert models.DocumentAccess.objects.get(document=doc, user=erin).role == "editor"
    # bob keeps his direct access, no longer managed
    assert models.DocumentAccess.objects.get(document=doc, user=bob).role == "editor"
    assert not ManagedAccess.objects.filter(access__user=bob).exists()
    assert mail.outbox == []

    # the grant is gone: erin loses her access
    response = admin_client.put(url, {"accesses": []}, format="json")
    assert response.json()["removed"] == ["erin@example.com"]
    assert not models.DocumentAccess.objects.filter(document=doc, user=erin).exists()
    assert outbox(events.ACCESS_DELETED)[-1].data["sub"] == "erin"


def test_one_access_per_person_and_document(alice, bob):
    """Docs refuses a second access of the same user on a document."""
    doc = factories.DocumentFactory(
        users=[(alice, "owner"), (bob, "reader")], link_reach="restricted"
    )
    with pytest.raises(Exception):  # noqa: B017  IntegrityError or ValidationError
        models.DocumentAccess.objects.create(document=doc, user=bob, role="editor")


def test_shared_drive_grant_admin_routes(alice, admin_client):
    """The side service poses and removes the grant of a shared drive."""
    doc = factories.DocumentFactory(users=[(alice, "owner")], link_reach="restricted")
    response = admin_client.post(
        f"/twake/documents/{doc.pk}/grants/",
        {"source_id": "drive-1", "name": "Équipe", "role": "editor", "member_count": 3},
        format="json",
    )
    assert response.status_code == 201, response.json()
    grant_id = response.json()["id"]
    assert Grant.objects.get(pk=grant_id).source_kind == "shared_drive"
    # idempotent: same drive again updates
    response = admin_client.post(
        f"/twake/documents/{doc.pk}/grants/",
        {"source_id": "drive-1", "name": "Équipe", "role": "editor", "member_count": 4},
        format="json",
    )
    assert response.status_code == 200
    assert Grant.objects.count() == 1
    response = admin_client.patch(
        f"/twake/grants/{grant_id}/", {"member_count": 5}, format="json"
    )
    assert response.json()["member_count"] == 5
    assert (
        admin_client.delete(f"/twake/documents/{doc.pk}/grants/{grant_id}/").status_code
        == 204
    )
    assert not Grant.objects.exists()
