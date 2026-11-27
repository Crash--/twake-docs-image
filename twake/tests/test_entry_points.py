"""`POST /twake/entry-points/`: the entry points of many people at once."""

import pytest

from core import factories, models

from twake.models import Grant, WorkplaceUser

pytestmark = pytest.mark.django_db

PLAN = "0b85f827-af0f-4696-991d-45566e4cedba"
SUBPAGE = "583dca89-a4a0-4d83-aab0-0549dae2c769"


@pytest.fixture
def erin():
    """A person with no access yet."""
    return factories.UserFactory(sub="erin", email="erin@docs-drive.test")


@pytest.fixture(autouse=True)
def instances(bob, erin):
    """The people of the contract, on their instances."""
    WorkplaceUser.objects.create(user=bob, workplace_fqdn="bob.example.net")
    WorkplaceUser.objects.create(user=erin, workplace_fqdn="erin.example.net")


@pytest.fixture
def plan(alice, bob):
    """A document of alice that bob reads, with a sub-page."""
    root = factories.DocumentFactory(
        id=PLAN,
        title="Plan",
        link_reach="restricted",
        users=[(alice, "owner"), (bob, "reader")],
    )
    factories.DocumentFactory(
        id=SUBPAGE, title="Sub", parent=root, link_reach="restricted"
    )
    return root


def ask(client, **payload):
    response = client.post("/twake/entry-points/", payload, format="json")
    assert response.status_code == 200, response.content
    return response.json()["results"]


def test_entry_points_within_a_tree_match_the_contract(admin_client, plan):
    """The example of the contract, section 6.2."""
    assert ask(admin_client, people=["bob", "erin"], within=PLAN) == {
        "bob": {
            "workplace_fqdn": "bob.example.net",
            "entry_points": [
                {
                    "id": PLAN,
                    "title": "Plan",
                    "role": "reader",
                    "deleted": False,
                    "drives": [],
                }
            ],
            "others": [SUBPAGE],
        },
        "erin": {
            "workplace_fqdn": "erin.example.net",
            "entry_points": [],
            "others": [PLAN, SUBPAGE],
        },
    }


def test_entry_points_without_within_list_everything_and_no_others(
    admin_client, alice, bob, plan
):
    """Every entry point, `others` empty."""
    own = factories.DocumentFactory(
        title="Mine", link_reach="restricted", users=[(bob, "owner")]
    )
    results = ask(admin_client, people=["bob"])
    ids = {e["id"]: e["role"] for e in results["bob"]["entry_points"]}
    assert ids == {PLAN: "reader", str(own.pk): "owner"}
    assert results["bob"]["others"] == []


def test_people_are_named_by_sub_or_email(admin_client, plan):
    """Both forms work, mixed. The key is the sub that was sent, or the email in
    lowercase; an unknown person is absent."""
    results = ask(
        admin_client, people=["Bob@Example.com", "erin", "nobody"], within=PLAN
    )
    assert sorted(results) == ["bob@example.com", "erin"]
    assert results["bob@example.com"]["entry_points"][0]["id"] == PLAN


def test_only_the_highest_document_is_an_entry_point(admin_client, bob, plan):
    """An access on a descendant is under the entry point of the ancestor, and
    the role is the strongest of the two (what `GET /documents/` shows)."""
    models.DocumentAccess.objects.create(document_id=SUBPAGE, user=bob, role="editor")
    entry = ask(admin_client, people=["bob"])["bob"]["entry_points"]
    assert [(e["id"], e["role"]) for e in entry] == [(PLAN, "reader")]


def test_a_descendant_shared_alone_is_an_entry_point_and_the_root_is_not(
    admin_client, erin, plan
):
    """P4: the sub-page is shared alone, so it is the entry point of erin."""
    models.DocumentAccess.objects.create(document_id=SUBPAGE, user=erin, role="editor")
    result = ask(admin_client, people=["erin"], within=PLAN)["erin"]
    assert [e["id"] for e in result["entry_points"]] == [SUBPAGE]
    assert result["others"] == [PLAN]


def test_within_a_subtree_sees_the_entry_point_of_an_ancestor_as_outside(
    admin_client, plan
):
    """Bob reaches the sub-page through the root: nothing is his entry point
    inside the sub-tree, and the sub-page is one of `others`."""
    result = ask(admin_client, people=["bob"], within=SUBPAGE)["bob"]
    assert result["entry_points"] == []
    assert result["others"] == [SUBPAGE]


def test_documents_in_the_trash_are_not_listed_but_the_owner_keeps_theirs(
    admin_client, alice, bob, plan
):
    """`GET /documents/` hides the trash: a reader loses the entry point, the
    owner gets it with `deleted`, so their pointer stays in the trash of Drive."""
    plan.soft_delete()
    assert ask(admin_client, people=["bob"])["bob"]["entry_points"] == []
    entry = ask(admin_client, people=["alice"])["alice"]["entry_points"]
    assert [(e["id"], e["role"], e["deleted"]) for e in entry] == [
        (PLAN, "owner", True)
    ]


def test_a_document_visited_by_a_public_link_is_listed_without_role(admin_client, erin):
    """Like the list of Docs: a link trace on a document that is not restricted."""
    doc = factories.DocumentFactory(title="Open", link_reach="public")
    models.LinkTrace.objects.create(document=doc, user=erin)
    restricted = factories.DocumentFactory(title="Closed", link_reach="restricted")
    models.LinkTrace.objects.create(document=restricted, user=erin)
    entry = ask(admin_client, people=["erin"])["erin"]["entry_points"]
    assert [(e["id"], e["role"]) for e in entry] == [(str(doc.pk), None)]


def test_drives_carry_the_grants_of_the_shared_drives(admin_client, plan):
    """`drives` names the drives that hold a grant on the document."""
    Grant.objects.create(
        document=plan,
        source_kind="shared_drive",
        source_id="b6108553462e0839383f0ee5400371e0",
        owner_instance="acme.example.net",
        role="editor",
    )
    Grant.objects.create(
        document=plan, source_kind="group", source_id="g", role="reader"
    )
    entry = ask(admin_client, people=["bob"])["bob"]["entry_points"][0]
    assert entry["drives"] == [
        {
            "source_id": "b6108553462e0839383f0ee5400371e0",
            "owner_instance": "acme.example.net",
        }
    ]


def test_at_most_200_people_and_the_service_token(admin_client, session_client):
    """The size of a batch, and the admin authentication."""
    too_many = [f"p{i}" for i in range(201)]
    response = admin_client.post(
        "/twake/entry-points/", {"people": too_many}, format="json"
    )
    assert response.status_code == 400
    response = session_client.post(
        "/twake/entry-points/", {"people": ["bob"]}, format="json"
    )
    assert response.status_code in (401, 403)


def test_a_batch_does_not_cost_one_query_per_person(
    admin_client, bob, plan, django_assert_max_num_queries
):
    """People are read together: the number of queries does not follow the
    number of people."""
    others = [factories.UserFactory(sub=f"u{i}") for i in range(20)]
    for user in others:
        models.DocumentAccess.objects.create(document=plan, user=user, role="reader")
    with django_assert_max_num_queries(15):
        results = ask(
            admin_client, people=[u.sub for u in others] + ["bob"], within=PLAN
        )
    assert len(results) == 21
