"""`GET /twake/documents/{id}/`: a document for the side service."""

import pytest

from core import factories, models

from twake.models import Grant, ManagedAccess, WorkplaceUser

pytestmark = pytest.mark.django_db

PLAN = "0b85f827-af0f-4696-991d-45566e4cedba"
SUBPAGE = "583dca89-a4a0-4d83-aab0-0549dae2c769"


@pytest.fixture
def plan(alice, bob):
    """The Plan of alice, read by bob, with a grant and a sub-page."""
    WorkplaceUser.objects.create(user=alice, workplace_fqdn="alice.example.net")
    WorkplaceUser.objects.create(user=bob, workplace_fqdn="bob.example.net")
    root = factories.DocumentFactory(
        id=PLAN,
        title="Plan",
        link_reach="restricted",
        users=[(alice, "owner"), (bob, "reader")],
    )
    Grant.objects.create(
        document=root,
        source_kind="group",
        source_id="b2b-group-7fa0",
        organization_id="acme",
        external_id="rh",
        role="reader",
        members_as_of="2026-11-27T10:00:00.000Z",
    )
    return root


def test_document_matches_the_contract(admin_client, plan):
    """The example of the contract, section 6.3."""
    response = admin_client.get(f"/twake/documents/{PLAN}/")
    assert response.status_code == 200
    body = response.json()
    grant = body["grants"][0]
    assert {k: v for k, v in body.items() if k not in ("grants", "tree_accessors")} == {
        "id": PLAN,
        "title": "Plan",
        "parent_id": None,
        "root_id": PLAN,
        "deleted": False,
        "owners": [
            {
                "sub": "alice",
                "email": "alice@example.com",
                "workplace_fqdn": "alice.example.net",
            }
        ],
        "accessors": [
            {
                "sub": "bob",
                "email": "bob@example.com",
                "workplace_fqdn": "bob.example.net",
                "managed": False,
            }
        ],
    }
    assert {
        k: grant[k]
        for k in (
            "source_kind",
            "source_id",
            "organization_id",
            "external_id",
            "owner_instance",
            "role",
            "members_as_of",
        )
    } == {
        "source_kind": "group",
        "source_id": "b2b-group-7fa0",
        "organization_id": "acme",
        "external_id": "rh",
        "owner_instance": None,
        "role": "reader",
        "members_as_of": "2026-11-27T10:00:00.000Z",
    }


def test_accessors_of_a_subpage_include_the_ancestors_and_flag_managed(
    admin_client, plan, bob
):
    """Accesses on an ancestor count, a granted access is `managed`, and a
    person with a direct access somewhere is not."""
    sub = factories.DocumentFactory(id=SUBPAGE, title="Sub", parent=plan)
    erin = factories.UserFactory(sub="erin", email="erin@docs-drive.test")
    access = models.DocumentAccess.objects.create(
        document=sub, user=erin, role="reader"
    )
    ManagedAccess.objects.create(access=access)

    body = admin_client.get(f"/twake/documents/{SUBPAGE}/").json()

    assert body["parent_id"] == PLAN
    assert body["root_id"] == PLAN
    assert {a["sub"]: a["managed"] for a in body["accessors"]} == {
        "bob": False,
        "erin": True,
    }
    assert [o["sub"] for o in body["owners"]] == ["alice"]
    assert {a["sub"]: a["workplace_fqdn"] for a in body["accessors"]} == {
        "bob": "bob.example.net",
        "erin": "",
    }


def test_tree_accessors_are_everyone_with_an_access_in_the_tree(
    admin_client, plan, alice
):
    """Corbeille and restore need the people of a sub-page shared apart."""
    sub = factories.DocumentFactory(id=SUBPAGE, title="Sub", parent=plan)
    erin = factories.UserFactory(sub="erin", email="erin@docs-drive.test")
    models.DocumentAccess.objects.create(document=sub, user=erin, role="editor")

    root = admin_client.get(f"/twake/documents/{PLAN}/").json()
    leaf = admin_client.get(f"/twake/documents/{SUBPAGE}/").json()

    assert {p["sub"] for p in root["tree_accessors"]} == {"alice", "bob", "erin"}
    assert {p["sub"] for p in leaf["tree_accessors"]} == {"erin"}


def test_a_direct_access_covered_by_a_grant_is_not_managed(admin_client, plan, bob):
    """Bob was given the document by hand, then a grant covered him too."""
    access = models.DocumentAccess.objects.get(document=plan, user=bob)
    ManagedAccess.objects.create(access=access, previous_role="reader")
    body = admin_client.get(f"/twake/documents/{PLAN}/").json()
    assert body["accessors"][0]["managed"] is False


def test_a_document_in_the_trash_is_read_with_deleted(admin_client, plan):
    plan.soft_delete()
    assert admin_client.get(f"/twake/documents/{PLAN}/").json()["deleted"] is True


def test_unknown_document_and_session(admin_client, session_client, plan):
    assert admin_client.get(f"/twake/documents/{SUBPAGE}/").status_code == 404
    assert session_client.get(f"/twake/documents/{PLAN}/").status_code in (401, 403)
