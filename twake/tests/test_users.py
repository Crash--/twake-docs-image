"""The users routes of the side service: instance, organization, settings date."""

import pytest

from core import factories

from twake.models import WorkplaceUser

pytestmark = pytest.mark.django_db


def test_post_users_keeps_the_instance_and_the_organization(admin_client):
    """The fixture of the contract, section 6.1."""
    response = admin_client.post(
        "/twake/users/",
        {
            "sub": "bob",
            "email": "bob@docs-drive.test",
            "workplace_fqdn": "bob.example.net",
            "organization_id": "acme",
        },
        format="json",
    )
    assert response.status_code == 201
    workplace = WorkplaceUser.objects.get(user__sub="bob")
    assert workplace.workplace_fqdn == "bob.example.net"
    assert workplace.organization_id == "acme"


def test_post_users_again_updates_the_instance_and_keeps_it_when_absent(admin_client):
    """A person who moves to another instance is republished; a call without
    the fields leaves them alone."""
    payload = {"sub": "bob", "email": "bob@docs-drive.test"}
    admin_client.post(
        "/twake/users/", {**payload, "workplace_fqdn": "old.example.net"}, format="json"
    )
    admin_client.post(
        "/twake/users/", {**payload, "workplace_fqdn": "bob.example.net"}, format="json"
    )
    admin_client.post("/twake/users/", payload, format="json")
    assert (
        WorkplaceUser.objects.get(user__sub="bob").workplace_fqdn == "bob.example.net"
    )


def test_settings_older_than_the_applied_ones_are_ignored(admin_client):
    """A late `user.settings.updated` does not bring a language back."""
    factories.UserFactory(sub="bob", email="bob@docs-drive.test", language="en-us")
    url = "/twake/users/settings/"
    newer = {
        "email": "bob@docs-drive.test",
        "language": "fr-fr",
        "updated_at": "2026-11-27T10:00:05.000Z",
    }
    older = {**newer, "language": "de-de", "updated_at": "2026-11-27T10:00:00.000Z"}

    assert admin_client.put(url, newer, format="json").json()["applied"] is True
    response = admin_client.put(url, older, format="json")

    assert response.status_code == 200
    assert response.json() == {"applied": False}
    workplace = WorkplaceUser.objects.get(user__sub="bob")
    assert workplace.user.language == "fr-fr"
    assert workplace.settings_at.isoformat().startswith("2026-11-27T10:00:05")


def test_settings_without_a_date_still_apply(admin_client):
    """The routes of the previous side service send no date."""
    factories.UserFactory(sub="bob", email="bob@docs-drive.test", language="en-us")
    response = admin_client.put(
        "/twake/users/settings/",
        {"email": "bob@docs-drive.test", "language": "fr-fr"},
        format="json",
    )
    assert response.json()["applied"] is True


def test_list_users_is_paginated_with_the_instance(admin_client):
    """The fixture of the contract, section 6.1, for the nightly pass."""
    factories.UserFactory(sub="bob", email="bob@docs-drive.test")
    factories.UserFactory(sub="erin", email="erin@docs-drive.test")
    WorkplaceUser.objects.create(
        user=factories.UserFactory(sub="gina", email="gina@docs-drive.test"),
        workplace_fqdn="gina.example.net",
    )

    response = admin_client.get("/twake/users/?page=1&page_size=2")

    body = response.json()
    assert response.status_code == 200
    assert body["count"] == 3
    assert len(body["results"]) == 2
    assert body["next"] is not None
    rest = admin_client.get(body["next"]).json()
    assert body["results"] + rest["results"] == [
        {"sub": "bob", "email": "bob@docs-drive.test", "workplace_fqdn": ""},
        {"sub": "erin", "email": "erin@docs-drive.test", "workplace_fqdn": ""},
        {
            "sub": "gina",
            "email": "gina@docs-drive.test",
            "workplace_fqdn": "gina.example.net",
        },
    ]


def test_list_users_caps_the_page_size(admin_client):
    """Never more than 200 people per page."""
    response = admin_client.get("/twake/users/?page_size=1000")
    assert response.status_code == 200
    assert response.json()["next"] is None
