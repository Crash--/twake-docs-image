"""The members of the grants, and the accesses Docs derives from them."""

from django.core import mail

import pytest

from core import factories, models

from twake import events
from twake.models import Grant, GrantMember, ManagedAccess

from .conftest import outbox

pytestmark = pytest.mark.django_db

T0 = "2026-11-27T10:00:00.000Z"
T1 = "2026-11-27T10:00:01.000Z"
T2 = "2026-11-27T10:00:02.000Z"
PLAN = "0b85f827-af0f-4696-991d-45566e4cedba"
GROUP = "b2b-group-7fa0a3f55d63964b97c1d79556dcfcb9"
DRIVE = "b6108553462e0839383f0ee5400371e0"

BOB = {
    "email": "bob@docs-drive.test",
    "sub": "bob",
    "workplace_fqdn": "bob.example.net",
    "read_only": False,
}
ERIN = {
    "email": "erin@docs-drive.test",
    "sub": "erin",
    "workplace_fqdn": "erin.example.net",
    "read_only": False,
}


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def bob_user():
    return factories.UserFactory(sub="bob", email=BOB["email"], language="en-us")


@pytest.fixture
def erin_user():
    return factories.UserFactory(sub="erin", email=ERIN["email"], language="en-us")


@pytest.fixture
def plan(alice):
    return factories.DocumentFactory(
        id=PLAN, title="Plan", link_reach="restricted", users=[(alice, "owner")]
    )


@pytest.fixture
def group(plan):
    return Grant.objects.create(
        document=plan,
        source_kind="group",
        source_id=GROUP,
        organization_id="acme",
        external_id="rh",
        role="reader",
    )


@pytest.fixture
def drive(plan):
    return Grant.objects.create(
        document=plan,
        source_kind="shared_drive",
        source_id=DRIVE,
        owner_instance="acme.example.net",
        role="editor",
    )


def put(client, grant, as_of, members):
    return client.put(
        f"/twake/grants/{grant.pk}/members/",
        {"as_of": as_of, "members": members},
        format="json",
    )


def post(client, at, add=(), remove=(), source=GROUP):
    return client.post(
        "/twake/grants/members/",
        {
            "source_kind": "group",
            "source_id": source,
            "at": at,
            "add": list(add),
            "remove": list(remove),
        },
        format="json",
    )


def role_of(user, document):
    access = models.DocumentAccess.objects.filter(document=document, user=user).first()
    return access.role if access else None


def test_put_members_poses_the_accesses_of_the_contract(
    admin_client, plan, group, bob_user
):
    """The example of the contract, section 6.4."""
    response = put(admin_client, group, T0, [BOB])

    assert response.status_code == 200
    assert response.json() == {
        "document_id": PLAN,
        "member_count": 1,
        "changed": [
            {
                "sub": "bob",
                "email": "bob@docs-drive.test",
                "workplace_fqdn": "bob.example.net",
                "role": "reader",
            }
        ],
        "removed": [],
        "unknown": [],
    }
    assert role_of(bob_user, plan) == "reader"
    assert ManagedAccess.objects.get(access__user=bob_user).previous_role == ""
    group.refresh_from_db()
    assert group.members_as_of.isoformat().startswith("2026-11-27T10:00:00")
    # The sharing email of Docs, as when a person is shared by hand
    assert [m.to for m in mail.outbox] == [[BOB["email"]]]


def test_put_members_is_idempotent(admin_client, group, bob_user):
    put(admin_client, group, T0, [BOB])
    response = put(admin_client, group, T0, [BOB])
    assert response.status_code == 200
    assert response.json()["changed"] == []
    assert len(mail.outbox) == 1


def test_a_list_older_than_the_last_one_is_refused(admin_client, group, bob_user):
    put(admin_client, group, T1, [BOB])
    response = put(admin_client, group, T0, [])
    assert response.status_code == 409
    assert GrantMember.objects.filter(grant=group, removed=False).count() == 1


def test_a_member_left_out_of_the_list_loses_the_access(
    admin_client, plan, group, bob_user, erin_user
):
    put(admin_client, group, T0, [BOB, ERIN])
    response = put(admin_client, group, T1, [ERIN])

    body = response.json()
    assert body["member_count"] == 1
    assert body["removed"] == [
        {
            "sub": "bob",
            "email": "bob@docs-drive.test",
            "workplace_fqdn": "bob.example.net",
        }
    ]
    assert role_of(bob_user, plan) is None
    assert role_of(erin_user, plan) == "reader"
    # The mark stays, a late addition cannot bring bob back
    assert GrantMember.objects.get(grant=group, email=BOB["email"]).removed


def test_a_member_added_while_the_full_list_was_read_stays(
    admin_client, plan, group, bob_user, erin_user
):
    """B5: erin joined at T1, the list was read at T0 without her."""
    post(admin_client, T1, add=[ERIN])
    response = put(admin_client, group, T0, [BOB])

    assert response.status_code == 200
    assert role_of(erin_user, plan) == "reader"
    assert role_of(bob_user, plan) == "reader"
    assert GrantMember.objects.filter(grant=group, removed=False).count() == 2


def test_post_members_matches_the_contract_on_every_grant_of_the_source(
    admin_client, alice, group, erin_user
):
    """The example of the contract, section 6.4, on two documents."""
    other = factories.DocumentFactory(
        title="Other", link_reach="restricted", users=[(alice, "owner")]
    )
    Grant.objects.create(
        document=other, source_kind="group", source_id=GROUP, role="editor"
    )

    response = post(admin_client, T1, add=[ERIN])

    assert response.status_code == 200
    documents = {d["document_id"]: d for d in response.json()["documents"]}
    assert set(documents) == {PLAN, str(other.pk)}
    assert documents[PLAN] == {
        "document_id": PLAN,
        "member_count": 1,
        "changed": [
            {
                "sub": "erin",
                "email": "erin@docs-drive.test",
                "workplace_fqdn": "erin.example.net",
                "role": "reader",
            }
        ],
        "removed": [],
    }
    assert role_of(erin_user, other) == "editor"


def test_the_member_count_of_the_grant_follows_the_members(
    admin_client, group, bob_user, erin_user
):
    """Story G1: the modal shows « n members » and nobody patches the grant."""
    put(admin_client, group, T0, [BOB, ERIN])
    group.refresh_from_db()
    assert group.member_count == 2

    post(admin_client, T1, remove=[ERIN])
    group.refresh_from_db()
    assert group.member_count == 1

    post(admin_client, T2, add=[ERIN])
    group.refresh_from_db()
    assert group.member_count == 2
    # No `grant.updated` for the side service to answer
    assert not outbox(events.GRANT_UPDATED)


def test_post_members_removes_and_unknown_source_does_nothing(
    admin_client, plan, group, erin_user
):
    post(admin_client, T0, add=[ERIN])
    response = post(admin_client, T1, remove=[ERIN])
    assert response.json()["documents"][0]["removed"][0]["sub"] == "erin"
    assert role_of(erin_user, plan) is None
    assert post(admin_client, T1, add=[ERIN], source="nothing").json() == {
        "documents": []
    }


def test_a_removal_arriving_before_an_older_addition_wins(
    admin_client, plan, group, erin_user
):
    """B4: `member.removed` (T2) delivered before `member.added` (T1)."""
    post(admin_client, T2, remove=[ERIN])
    post(admin_client, T1, add=[ERIN])
    assert role_of(erin_user, plan) is None
    assert GrantMember.objects.get(grant=group).removed


def test_an_addition_arriving_before_an_older_removal_wins(
    admin_client, plan, group, erin_user
):
    post(admin_client, T2, add=[ERIN])
    post(admin_client, T1, remove=[ERIN])
    assert role_of(erin_user, plan) == "reader"


def test_a_change_older_than_the_full_list_is_ignored(
    admin_client, plan, group, erin_user
):
    """The list read at T1 does not have erin: an addition of T0 is stale."""
    put(admin_client, group, T1, [])
    post(admin_client, T0, add=[ERIN])
    assert role_of(erin_user, plan) is None
    assert not GrantMember.objects.filter(grant=group).exists()


def test_a_direct_access_wins_over_the_grants(admin_client, plan, group, bob_user):
    """The strongest role stays, and the direct role comes back with the grant."""
    models.DocumentAccess.objects.create(document=plan, user=bob_user, role="editor")

    put(admin_client, group, T0, [BOB])
    assert role_of(bob_user, plan) == "editor"
    assert ManagedAccess.objects.get(access__user=bob_user).previous_role == "editor"

    put(admin_client, group, T1, [])
    assert role_of(bob_user, plan) == "editor"
    assert not ManagedAccess.objects.exists()


def test_a_grant_stronger_than_the_direct_access_raises_it_and_gives_it_back(
    admin_client, plan, drive, bob_user
):
    """Bob reads by hand and the drive makes him editor: the strongest role
    applies while he is a member, his own role comes back after."""
    models.DocumentAccess.objects.create(document=plan, user=bob_user, role="reader")

    put(admin_client, drive, T0, [BOB])
    assert role_of(bob_user, plan) == "editor"

    put(admin_client, drive, T1, [])
    assert role_of(bob_user, plan) == "reader"
    assert not ManagedAccess.objects.exists()


def test_the_strongest_role_of_two_grants_wins(
    admin_client, plan, group, drive, bob_user
):
    put(admin_client, group, T0, [BOB])
    response = put(admin_client, drive, T0, [BOB])

    # Only the role changed: bob is still reported
    assert [(p["sub"], p["role"]) for p in response.json()["changed"]] == [
        ("bob", "editor")
    ]
    assert role_of(bob_user, plan) == "editor"

    put(admin_client, drive, T1, [])
    assert role_of(bob_user, plan) == "reader"


def test_a_read_only_member_of_a_drive_is_a_reader(
    admin_client, plan, drive, bob_user, erin_user
):
    response = put(admin_client, drive, T0, [{**BOB, "read_only": True}, ERIN])
    assert {p["sub"]: p["role"] for p in response.json()["changed"]} == {
        "bob": "reader",
        "erin": "editor",
    }
    assert role_of(bob_user, plan) == "reader"


def test_the_owner_is_left_out(admin_client, plan, group, alice):
    alice_member = {"email": alice.email, "sub": "alice", "read_only": False}
    response = put(admin_client, group, T0, [alice_member])
    assert response.json()["changed"] == []
    assert not ManagedAccess.objects.exists()
    assert role_of(alice, plan) == "owner"


def test_a_person_unknown_to_docs_is_invited(admin_client, plan, group):
    response = put(admin_client, group, T0, [BOB])
    assert response.json()["unknown"] == [BOB["email"]]
    assert models.Invitation.objects.get().email == BOB["email"]
    assert [m.to for m in mail.outbox] == [[BOB["email"]]]


def test_changing_the_role_of_a_grant_changes_the_accesses(
    session_client, admin_client, plan, group, bob_user
):
    put(admin_client, group, T0, [BOB])
    response = session_client.patch(
        f"/api/v1.0/twake/documents/{PLAN}/grants/{group.pk}/",
        {"role": "editor"},
        format="json",
    )
    assert response.status_code == 200
    assert role_of(bob_user, plan) == "editor"


def test_removing_a_grant_removes_its_members_and_takes_back_the_accesses(
    admin_client, group, drive, bob_user, erin_user
):
    plan = group.document
    put(admin_client, group, T0, [BOB, ERIN])
    put(admin_client, drive, T0, [BOB])

    response = admin_client.delete(f"/twake/documents/{PLAN}/grants/{drive.pk}/")
    assert response.status_code == 204
    assert role_of(bob_user, plan) == "reader"

    response = admin_client.delete(f"/twake/documents/{PLAN}/grants/{group.pk}/")
    assert response.status_code == 204
    assert role_of(bob_user, plan) is None
    assert role_of(erin_user, plan) is None
    assert not GrantMember.objects.exists()


def test_grant_deleted_carries_the_members_read_before_the_deletion(
    admin_client, plan, group, bob_user, erin_user
):
    put(admin_client, group, T0, [BOB, ERIN])
    put(admin_client, group, T1, [ERIN])
    admin_client.delete(f"/twake/documents/{PLAN}/grants/{group.pk}/")

    data = outbox(events.GRANT_DELETED)[-1].data
    assert data["members"] == [
        {
            "sub": "erin",
            "email": "erin@docs-drive.test",
            "workplace_fqdn": "erin.example.net",
        }
    ]


def test_moving_a_document_gives_back_the_accesses_of_its_grants(
    session_client, admin_client, alice, group, bob_user
):
    """M4: Docs wipes the direct accesses of a document that moves."""
    plan = group.document
    put(admin_client, group, T0, [BOB])
    target = factories.DocumentFactory(
        title="Target", link_reach="restricted", users=[(alice, "owner")]
    )

    response = session_client.post(
        f"/api/v1.0/documents/{PLAN}/move/",
        {"target_document_id": str(target.pk), "position": "last-child"},
        format="json",
    )

    assert response.status_code == 200, response.content
    assert role_of(bob_user, plan) == "reader"
    assert ManagedAccess.objects.filter(access__user=bob_user).exists()
    # No second sharing email: only the one of the first list
    assert len(mail.outbox) == 1
