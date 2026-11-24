"""Publishes the outbox on RabbitMQ, as CloudEvents on a topic exchange."""

import logging
import time

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from kombu import Connection, Exchange

from .events import SOURCE
from .models import Outbox

logger = logging.getLogger(__name__)

BATCH = 100


def cloud_event(row):
    """The CloudEvent 1.0 of an outbox row, structured JSON mode."""
    event = {
        "specversion": "1.0",
        "id": str(row.id),
        "source": SOURCE,
        "type": row.type,
        "time": row.time.isoformat(),
        "data": row.data,
    }
    if row.actor:
        event["twakeactor"] = row.actor
    return event


class Relay:
    """One relay: one AMQP connection, pending rows published in order."""

    def __init__(self, url=None, exchange=None):
        self.url = url or settings.TWAKE_AMQP_URL
        self.exchange = Exchange(
            exchange or settings.TWAKE_AMQP_EXCHANGE, type="topic", durable=True
        )
        self.connection = None

    def producer(self):
        """A producer on a connection opened once, reopened after a failure."""
        if self.connection is None:
            self.connection = Connection(self.url)
            self.connection.ensure_connection(max_retries=3)
        return self.connection.Producer()

    def publish_pending(self):
        """Publish the pending rows in order; stop at the first failure.

        Returns the number of rows published.
        """
        published = 0
        with transaction.atomic():
            rows = list(
                Outbox.objects.select_for_update(skip_locked=True)
                .filter(sent_at__isnull=True)
                .order_by("time", "id")[:BATCH]
            )
            if not rows:
                return 0
            producer = self.producer()
            for row in rows:
                event = cloud_event(row)
                producer.publish(
                    event,
                    exchange=self.exchange,
                    routing_key=row.type,
                    message_id=str(row.id),
                    delivery_mode=2,
                    serializer="json",
                    declare=[self.exchange],
                    retry=False,
                )
                row.sent_at = timezone.now()
                row.save(update_fields=["sent_at"])
                published += 1
        return published

    def run_forever(self, interval=0.5):
        """Poll the outbox until the process is stopped."""
        while True:
            try:
                n = self.publish_pending()
            except Exception:
                logger.exception("outbox relay: publication failed, retrying")
                self.close()
                # A broken database connection is not reused by Django
                connection.close()
                n = 0
            if n < BATCH:
                time.sleep(interval)

    def close(self):
        """Drop the connection; the next publication reopens it."""
        if self.connection is not None:
            try:
                self.connection.release()
            finally:
                self.connection = None
