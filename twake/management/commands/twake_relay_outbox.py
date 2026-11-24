"""Run the outbox relay: `python manage.py twake_relay_outbox`."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from twake.relay import Relay


class Command(BaseCommand):
    """Publish the events of Docs on RabbitMQ, forever or once."""

    help = "Publish the outbox of the twake app on RabbitMQ"

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true", help="publish what is pending, then exit"
        )

    def handle(self, *args, **options):
        if not settings.TWAKE_AMQP_URL:
            raise CommandError("TWAKE_AMQP_URL is not set")
        relay = Relay()
        if options["once"]:
            n = relay.publish_pending()
            self.stdout.write(f"published {n} event(s)")
            relay.close()
            return
        relay.run_forever()
