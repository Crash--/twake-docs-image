"""Twake app: what Twake Workplace needs from Docs, without touching its code."""

from django.apps import AppConfig


class TwakeConfig(AppConfig):
    """Django app configuration."""

    name = "twake"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # pylint: disable=import-outside-toplevel, unused-import
        from . import events  # noqa: PLC0415
