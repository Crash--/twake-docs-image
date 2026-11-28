"""`POST /twake/documents/{id}/gestures/` and `POST /twake/copies/`: the date of
the last gesture applied to a document, and the key of a copy."""

import uuid

import pytest

from core import factories

from twake.models import DocumentGestures

pytestmark = pytest.mark.django_db

T1 = "2026-11-28T10:00:00Z"
T2 = "2026-11-28T10:01:00Z"
T3 = "2026-11-28T10:02:00Z"


@pytest.fixture
def doc(alice):
    return factories.DocumentFactory(title="Plan", users=[(alice, "owner")])


def gesture(client, doc, **payload):
    return client.post(f"/twake/documents/{doc.pk}/gestures/", payload, format="json")


def date_of(doc, column):
    return getattr(DocumentGestures.objects.get(document=doc), column)


@pytest.mark.parametrize(
    "field,column", [("title", "title_at"), ("trash", "trash_at"), ("move", "moved_at")]
)
def test_the_first_gesture_is_accepted_and_recorded_on_demand(
    admin_client, doc, field, column
):
    response = gesture(admin_client, doc, field=field, as_of=T1)
    assert (response.status_code, response.json()) == (200, {"applied": True})
    assert date_of(doc, column) is None
    response = gesture(admin_client, doc, field=field, as_of=T1, record=True)
    assert (response.status_code, response.json()) == (200, {"applied": True})
    assert date_of(doc, column).isoformat().startswith("2026-11-28T10:00:00")


def test_an_older_or_equal_gesture_is_refused_a_newer_one_accepted(admin_client, doc):
    gesture(admin_client, doc, field="title", as_of=T2, record=True)
    for older in (T1, T2):
        response = gesture(admin_client, doc, field="title", as_of=older)
        assert (response.status_code, response.json()) == (409, {"applied": False})
    assert gesture(admin_client, doc, field="title", as_of=T3).status_code == 200
    assert date_of(doc, "title_at").isoformat().startswith("2026-11-28T10:01:00")


def test_the_dates_of_the_fields_are_independent(admin_client, doc):
    gesture(admin_client, doc, field="trash", as_of=T2, record=True)
    assert gesture(admin_client, doc, field="title", as_of=T1).status_code == 200
    assert gesture(admin_client, doc, field="move", as_of=T1).status_code == 200


def test_the_record_follows_a_gesture_that_dated_the_field_to_now(
    admin_client, doc, session_client
):
    """The side service renames through the API of Docs, which dates `title_at`
    to now, then records the date of the gesture."""
    assert gesture(admin_client, doc, field="title", as_of=T1).status_code == 200
    session_client.patch(
        f"/api/v1.0/documents/{doc.pk}/", {"title": "Renamed"}, format="json"
    )
    assert gesture(admin_client, doc, field="title", as_of=T1, record=True).status_code == 200
    assert date_of(doc, "title_at").isoformat().startswith("2026-11-28T10:00:00")


def test_a_restoration_then_an_older_trash_leaves_the_document_restored(
    admin_client, doc
):
    """Delivered out of order: the restoration (10:01) comes first, so the trash
    (10:00) is refused."""
    assert gesture(admin_client, doc, field="trash", as_of=T2, record=True).status_code == 200
    assert gesture(admin_client, doc, field="trash", as_of=T1).status_code == 409


def test_a_trash_then_an_older_restoration_is_refused(admin_client, doc):
    gesture(admin_client, doc, field="trash", as_of=T2, record=True)
    assert gesture(admin_client, doc, field="trash", as_of=T1).status_code == 409


def test_an_unknown_document_is_a_404(admin_client):
    response = admin_client.post(
        f"/twake/documents/{uuid.uuid4()}/gestures/",
        {"field": "title", "as_of": T1},
        format="json",
    )
    assert response.status_code == 404


def test_the_payload_is_checked(admin_client, doc):
    assert gesture(admin_client, doc, field="other", as_of=T1).status_code == 400
    assert gesture(admin_client, doc, field="title").status_code == 400
    assert gesture(admin_client, doc, field="copied_from").status_code == 400


def test_the_service_token_is_required(session_client, doc):
    assert gesture(session_client, doc, field="title", as_of=T1).status_code in (401, 403)


def test_a_copy_key_is_absent_then_set_then_given_back(admin_client, doc):
    ask = lambda: admin_client.post(  # noqa: E731
        "/twake/copies/", {"key": "alice.example.net:file-1"}, format="json"
    )
    assert ask().status_code == 404
    response = gesture(
        admin_client, doc, field="copied_from", key="alice.example.net:file-1"
    )
    assert response.status_code == 200
    assert ask().status_code == 404
    gesture(
        admin_client,
        doc,
        field="copied_from",
        key="alice.example.net:file-1",
        record=True,
    )
    response = ask()
    assert (response.status_code, response.json()) == (200, {"id": str(doc.pk)})


def test_a_copy_key_taken_by_another_document_is_refused(admin_client, doc, alice):
    other = factories.DocumentFactory(users=[(alice, "owner")])
    key = {"field": "copied_from", "key": "alice.example.net:file-1", "record": True}
    assert gesture(admin_client, doc, **key).status_code == 200
    assert gesture(admin_client, doc, **key).status_code == 200
    response = gesture(admin_client, other, **key)
    assert (response.status_code, response.json()) == (409, {"applied": False})
    assert DocumentGestures.objects.get(document=other).copied_from is None
