"""A title or a trash state changed in Docs dates the gesture to now."""

import pytest

from core import factories

from twake.models import DocumentGestures

pytestmark = pytest.mark.django_db

DOCUMENTS = "/api/v1.0/documents/"


@pytest.fixture
def doc(alice):
    return factories.DocumentFactory(title="Plan", users=[(alice, "owner")])


def dates(doc):
    row = DocumentGestures.objects.filter(document=doc).first()
    return (row.title_at, row.trash_at) if row else (None, None)


def test_a_new_document_has_no_date(doc):
    assert dates(doc) == (None, None)


def test_a_new_title_dates_the_title(doc, session_client):
    session_client.patch(f"{DOCUMENTS}{doc.pk}/", {"title": "Other"}, format="json")
    title_at, trash_at = dates(doc)
    assert title_at is not None
    assert trash_at is None


def test_the_same_title_dates_nothing(doc, session_client):
    session_client.patch(f"{DOCUMENTS}{doc.pk}/", {"title": "Plan"}, format="json")
    assert dates(doc) == (None, None)


def test_a_save_of_another_field_dates_nothing(doc):
    doc.link_reach = "public"
    doc.save(update_fields=["link_reach"])
    assert dates(doc) == (None, None)


def test_trash_and_restoration_date_the_trash(doc, session_client):
    assert session_client.delete(f"{DOCUMENTS}{doc.pk}/").status_code == 204
    _, trashed_at = dates(doc)
    assert trashed_at is not None
    assert session_client.post(f"{DOCUMENTS}{doc.pk}/restore/").status_code == 200
    _, restored_at = dates(doc)
    assert restored_at > trashed_at
    assert dates(doc)[0] is None
