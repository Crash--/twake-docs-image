"""The outbox relay: CloudEvents, published in order, marked sent."""

from unittest import mock

import pytest

from twake import events
from twake.models import Outbox
from twake.relay import Relay, cloud_event

pytestmark = pytest.mark.django_db


def test_cloud_event_shape():
    """Structured JSON mode, source twake://docs, the actor as extension."""
    row = Outbox.objects.create(
        type=events.DOCUMENT_CREATED, actor="alice", data={"id": "d1"}
    )
    event = cloud_event(row)
    assert event["specversion"] == "1.0"
    assert event["id"] == str(row.id)
    assert event["source"] == "twake://docs"
    assert event["type"] == events.DOCUMENT_CREATED
    assert event["twakeactor"] == "alice"
    assert event["data"] == {"id": "d1"}
    assert event["time"].startswith(str(row.time.year))


def test_publish_pending_in_order_and_mark_sent():
    """Rows are published oldest first, with their id as message id, once."""
    first = Outbox.objects.create(type="t.first", data={"n": 1})
    second = Outbox.objects.create(type="t.second", data={"n": 2})
    producer = mock.Mock()
    relay = Relay(url="memory://", exchange="activity")
    relay.producer = lambda: producer

    assert relay.publish_pending() == 2
    calls = producer.publish.call_args_list
    assert [c.kwargs["routing_key"] for c in calls] == ["t.first", "t.second"]
    assert [c.kwargs["message_id"] for c in calls] == [str(first.id), str(second.id)]
    assert calls[0].kwargs["delivery_mode"] == 2
    assert calls[0].kwargs["serializer"] == "json"
    assert not Outbox.objects.filter(sent_at__isnull=True).exists()
    assert relay.publish_pending() == 0
    assert producer.publish.call_count == 2


def test_a_failure_leaves_the_rows_pending():
    """The first failure stops the batch; nothing is marked sent."""
    Outbox.objects.create(type="t.first", data={"n": 1})
    producer = mock.Mock()
    producer.publish.side_effect = OSError("broker down")
    relay = Relay(url="memory://", exchange="activity")
    relay.producer = lambda: producer

    with pytest.raises(OSError):
        relay.publish_pending()
    assert Outbox.objects.filter(sent_at__isnull=True).count() == 1
