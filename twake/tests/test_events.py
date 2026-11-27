"""The outbox: one row per change of a document, an access or an invitation."""

import pytest

from core import factories, models

from twake import events
from twake.models import Grant, ManagedAccess, WorkplaceUser

from .conftest import outbox

pytestmark = pytest.mark.django_db

DOCUMENTS = "/api/v1.0/documents/"


def test_document_lifecycle_events(alice, session_client):
    """Create, rename, trash, restore: one event each, with the actor."""
    response = session_client.post(DOCUMENTS, {"title": "A"}, format="json")
    assert response.status_code == 201
    doc_id = response.json()["id"]
    created = outbox(events.DOCUMENT_CREATED)
    assert [r.data["id"] for r in created] == [doc_id]
    assert created[0].data["title"] == "A"
    assert created[0].data["parent_id"] is None
    assert created[0].actor == "alice"
    # the owner access of the creator
    assert [r.data["sub"] for r in outbox(events.ACCESS_CREATED)] == ["alice"]

    session_client.patch(f"{DOCUMENTS}{doc_id}/", {"title": "B"}, format="json")
    assert outbox(events.DOCUMENT_UPDATED)[-1].data["title"] == "B"

    assert session_client.delete(f"{DOCUMENTS}{doc_id}/").status_code == 204
    assert [r.data["id"] for r in outbox(events.DOCUMENT_TRASHED)] == [doc_id]

    assert session_client.post(f"{DOCUMENTS}{doc_id}/restore/").status_code == 200
    assert [r.data["id"] for r in outbox(events.DOCUMENT_RESTORED)] == [doc_id]


def test_subpage_and_move_events(alice, session_client):
    """A subpage carries its parent; a move, which emits no signal, is caught by
    the middleware."""
    a = factories.DocumentFactory(
        users=[(alice, "owner")], title="A", link_reach="restricted"
    )
    b = factories.DocumentFactory(
        users=[(alice, "owner")], title="B", link_reach="restricted"
    )
    response = session_client.post(
        f"{DOCUMENTS}{a.pk}/children/", {"title": "A1"}, format="json"
    )
    assert response.status_code == 201
    a1 = response.json()["id"]
    created = outbox(events.DOCUMENT_CREATED)[-1]
    assert created.data["id"] == a1
    assert created.data["parent_id"] == str(a.pk)

    response = session_client.post(
        f"{DOCUMENTS}{a1}/move/",
        {"target_document_id": str(b.pk), "position": "first-child"},
        format="json",
    )
    assert response.status_code == 200
    moved = outbox(events.DOCUMENT_MOVED)
    assert [r.data["id"] for r in moved] == [a1]
    assert moved[0].data["parent_id"] == str(b.pk)


def test_access_and_invitation_events(alice, bob, session_client):
    """Accesses and invitations: created, updated, deleted."""
    doc = factories.DocumentFactory(
        users=[(alice, "owner")], title="A", link_reach="restricted"
    )
    response = session_client.post(
        f"{DOCUMENTS}{doc.pk}/accesses/",
        {"user_id": str(bob.pk), "role": "reader"},
        format="json",
    )
    assert response.status_code == 201
    access_id = response.json()["id"]
    created = outbox(events.ACCESS_CREATED)[-1]
    assert created.data == {
        "document_id": str(doc.pk),
        "access_id": access_id,
        "sub": "bob",
        "email": "bob@example.com",
        "workplace_fqdn": "",
        "team": None,
        "role": "reader",
        "managed": False,
    }

    session_client.patch(
        f"{DOCUMENTS}{doc.pk}/accesses/{access_id}/", {"role": "editor"}, format="json"
    )
    assert outbox(events.ACCESS_UPDATED)[-1].data["role"] == "editor"

    session_client.delete(f"{DOCUMENTS}{doc.pk}/accesses/{access_id}/")
    assert outbox(events.ACCESS_DELETED)[-1].data["sub"] == "bob"

    response = session_client.post(
        f"{DOCUMENTS}{doc.pk}/invitations/",
        {"email": "carol@example.com", "role": "editor"},
        format="json",
    )
    assert response.status_code == 201
    invitation = outbox(events.INVITATION_CREATED)[-1]
    assert invitation.data["email"] == "carol@example.com"
    assert invitation.data["role"] == "editor"
    session_client.delete(f"{DOCUMENTS}{doc.pk}/invitations/{response.json()['id']}/")
    assert outbox(events.INVITATION_DELETED)[-1].data["email"] == "carol@example.com"


def test_link_change_is_a_document_update(alice, session_client):
    """The link reach and role travel in document.updated."""
    doc = factories.DocumentFactory(
        users=[(alice, "owner")], title="A", link_reach="restricted"
    )
    response = session_client.put(
        f"{DOCUMENTS}{doc.pk}/link-configuration/",
        {"link_reach": "public", "link_role": "reader"},
        format="json",
    )
    assert response.status_code == 200
    updated = outbox(events.DOCUMENT_UPDATED)[-1]
    assert updated.data["link_reach"] == "public"
    assert updated.data["link_role"] == "reader"


def test_grant_events(alice):
    """A grant written by the twake app has its own events."""
    doc = factories.DocumentFactory(users=[(alice, "owner")], link_reach="restricted")
    grant = Grant.objects.create(
        document=doc,
        source_kind="group",
        source_id="g1",
        name="Marketing",
        role="reader",
    )
    assert outbox(events.GRANT_CREATED)[-1].data["source_id"] == "g1"
    grant.role = "editor"
    grant.save()
    assert outbox(events.GRANT_UPDATED)[-1].data["role"] == "editor"
    grant_id = str(grant.pk)
    grant.delete()
    assert outbox(events.GRANT_DELETED)[-1].data["grant_id"] == grant_id


def test_purge_event(alice):
    """A hard delete emits document.deleted for the subtree."""
    a = factories.DocumentFactory(
        users=[(alice, "owner")], title="A", link_reach="restricted"
    )
    a1 = factories.DocumentFactory(parent=a, title="A1")
    models.Document.objects.get(pk=a.pk).delete()
    assert {r.data["id"] for r in outbox(events.DOCUMENT_DELETED)} == {
        str(a.pk),
        str(a1.pk),
    }


def test_access_events_carry_the_instance_and_the_managed_mark(alice, admin_client):
    """The example of the contract, section 6.5: a granted access is `managed`
    in its three events, `deleted` included."""
    bob = factories.UserFactory(sub="bob", email="bob@docs-drive.test")
    WorkplaceUser.objects.create(user=bob, workplace_fqdn="bob.example.net")
    doc = factories.DocumentFactory(
        users=[(alice, "owner")], title="Plan", link_reach="restricted"
    )
    grant = Grant.objects.create(
        document=doc, source_kind="group", source_id="g1", role="reader"
    )
    member = {"email": "bob@docs-drive.test", "sub": "bob", "read_only": False}
    url = f"/twake/grants/{grant.pk}/members/"

    admin_client.put(
        url, {"as_of": "2026-11-27T10:00:00.000Z", "members": [member]}, format="json"
    )
    created = outbox(events.ACCESS_CREATED)[-1]
    assert created.data == {
        "document_id": str(doc.pk),
        "access_id": created.data["access_id"],
        "sub": "bob",
        "email": "bob@docs-drive.test",
        "workplace_fqdn": "bob.example.net",
        "team": None,
        "role": "reader",
        "managed": True,
    }

    admin_client.put(
        url, {"as_of": "2026-11-27T10:00:01.000Z", "members": []}, format="json"
    )
    deleted = outbox(events.ACCESS_DELETED)[-1]
    assert deleted.data["access_id"] == created.data["access_id"]
    assert deleted.data["managed"] is True
    assert deleted.data["workplace_fqdn"] == "bob.example.net"


def test_access_deleted_by_hand_is_not_managed(alice, bob):
    doc = factories.DocumentFactory(
        users=[(alice, "owner")], title="A", link_reach="restricted"
    )
    access = models.DocumentAccess.objects.create(document=doc, user=bob, role="reader")
    access.delete()
    assert outbox(events.ACCESS_DELETED)[-1].data["managed"] is False


def test_access_covered_by_a_grant_but_held_directly_is_not_managed(alice, bob):
    """A person who also has the document by hand: their gestures are not
    ignored, even when a grant covers them."""
    doc = factories.DocumentFactory(
        users=[(alice, "owner")], title="A", link_reach="restricted"
    )
    access = models.DocumentAccess.objects.create(document=doc, user=bob, role="reader")
    ManagedAccess.objects.create(access=access, previous_role="reader")
    access.delete()
    assert outbox(events.ACCESS_DELETED)[-1].data["managed"] is False


def test_accesses_taken_back_with_a_grant_are_managed(alice, admin_client):
    """The `access.deleted` that follow `grant.deleted` are managed, and the
    grant names the members to recompute."""
    bob = factories.UserFactory(sub="bob", email="bob@docs-drive.test")
    doc = factories.DocumentFactory(
        users=[(alice, "owner")], title="Plan", link_reach="restricted"
    )
    grant = Grant.objects.create(
        document=doc, source_kind="group", source_id="g1", role="reader"
    )
    member = {"email": bob.email, "sub": "bob", "read_only": False}
    admin_client.put(
        f"/twake/grants/{grant.pk}/members/",
        {"as_of": "2026-11-27T10:00:00.000Z", "members": [member]},
        format="json",
    )

    admin_client.delete(f"/twake/documents/{doc.pk}/grants/{grant.pk}/")

    assert outbox(events.ACCESS_DELETED)[-1].data["managed"] is True
    assert [m["sub"] for m in outbox(events.GRANT_DELETED)[-1].data["members"]] == [
        "bob"
    ]
