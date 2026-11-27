"""The fields a grant keeps for the side service, and the route that lists them."""

import pytest

from core import factories

from twake import events
from twake.models import Grant

from .conftest import outbox

pytestmark = pytest.mark.django_db

API = "/api/v1.0/twake/"
GROUP = "b2b-group-7fa0a3f55d63964b97c1d79556dcfcb9"
DRIVE = "b6108553462e0839383f0ee5400371e0"


@pytest.fixture
def doc(alice):
    return factories.DocumentFactory(
        title="Plan", link_reach="restricted", users=[(alice, "owner")]
    )


def test_the_modal_keeps_the_organization_and_the_ldap_id_of_a_group(
    doc, session_client
):
    """Section 6.4: the fields of the group in the contract of `/contacts/suggest`."""
    response = session_client.post(
        f"{API}documents/{doc.pk}/grants/",
        {
            "source_kind": "group",
            "source_id": GROUP,
            "name": "Équipe RH",
            "role": "reader",
            "organization_id": "acme",
            "external_id": "rh",
        },
        format="json",
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["organization_id"], body["external_id"]) == ("acme", "rh")
    assert body["owner_instance"] is None
    assert body["members_as_of"] is None
    grant = Grant.objects.get(pk=body["id"])
    assert (grant.organization_id, grant.external_id) == ("acme", "rh")


def test_the_admin_route_keeps_the_owner_instance_of_a_drive(doc, admin_client):
    """A drive grant posed by the side service names the instance of the drive."""
    response = admin_client.post(
        f"/twake/documents/{doc.pk}/grants/",
        {
            "source_id": DRIVE,
            "role": "editor",
            "owner_instance": "acme.example.net",
        },
        format="json",
    )
    assert response.status_code == 201
    assert response.json()["owner_instance"] == "acme.example.net"
    assert Grant.objects.get().owner_instance == "acme.example.net"


def test_grant_events_carry_the_group_and_the_drive(doc):
    """Section 6.5: `organization_id`, `external_id`, `owner_instance`."""
    Grant.objects.create(
        document=doc,
        source_kind="group",
        source_id=GROUP,
        role="reader",
        organization_id="acme",
        external_id="rh",
    )
    Grant.objects.create(
        document=doc,
        source_kind="shared_drive",
        source_id=DRIVE,
        role="editor",
        owner_instance="acme.example.net",
    )
    group, drive = [r.data for r in outbox(events.GRANT_CREATED)]
    assert (group["organization_id"], group["external_id"]) == ("acme", "rh")
    assert group["owner_instance"] is None
    assert drive["owner_instance"] == "acme.example.net"


def test_list_grants_filters_by_source_and_paginates(doc, admin_client):
    """Section 6.4, with the filter of the side service and the nightly pass."""
    other = factories.DocumentFactory(title="Other", link_reach="restricted")
    for document in (doc, other):
        Grant.objects.create(
            document=document,
            source_kind="group",
            source_id=GROUP,
            role="reader",
            organization_id="acme",
            external_id="rh",
            members_as_of="2026-11-27T10:00:00.000Z",
        )
    Grant.objects.create(
        document=doc, source_kind="shared_drive", source_id=DRIVE, role="editor"
    )

    response = admin_client.get(f"/twake/grants/?source_kind=group&source_id={GROUP}")
    body = response.json()
    assert response.status_code == 200
    assert body["count"] == 2
    assert {g["document"] for g in body["results"]} == {str(doc.pk), str(other.pk)}
    first = body["results"][0]
    assert first["organization_id"] == "acme"
    assert first["external_id"] == "rh"
    assert first["members_as_of"] == "2026-11-27T10:00:00.000Z"

    everything = admin_client.get("/twake/grants/?page_size=2").json()
    assert (everything["count"], len(everything["results"])) == (3, 2)
    assert everything["next"] is not None


def test_list_grants_needs_the_service_token(session_client):
    assert session_client.get("/twake/grants/").status_code in (401, 403)
