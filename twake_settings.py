"""Settings of the Twake image of Docs: the upstream classes plus the twake app.

Selected with DJANGO_SETTINGS_MODULE=twake_settings, the upstream code is not
modified.
"""

from configurations import values
from impress import settings as impress


class TwakeMixin:
    """What the twake app needs from the settings."""

    ROOT_URLCONF = "twake.urls"

    # 30 days instead of the 7 upstream: a recipient who has no Docs account yet
    # gets it from the Workplace, days later (story I4).
    INVITATION_VALIDITY_DURATION = values.PositiveIntegerValue(
        30 * 24 * 3600,
        environ_name="TWAKE_INVITATION_VALIDITY_DURATION",
        environ_prefix=None,
    )
    # Where the outbox relay publishes the events of Docs. Empty: no relay.
    TWAKE_AMQP_URL = values.Value(
        "", environ_name="TWAKE_AMQP_URL", environ_prefix=None
    )
    TWAKE_AMQP_EXCHANGE = values.Value(
        "activity", environ_name="TWAKE_AMQP_EXCHANGE", environ_prefix=None
    )

    @classmethod
    def pre_setup(cls):
        """Add the twake app and its authentication to the upstream settings.

        Before setup: django-configurations copies the attributes to the settings
        module between pre_setup and post_setup, so a rebinding must happen here.
        """
        super().pre_setup()
        if "twake" not in cls.INSTALLED_APPS:
            cls.INSTALLED_APPS = [*cls.INSTALLED_APPS, "twake"]
        cls.REST_FRAMEWORK = {
            **cls.REST_FRAMEWORK,
            "DEFAULT_AUTHENTICATION_CLASSES": (
                "twake.authentication.OnBehalfAuthentication",
                *cls.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"],
            ),
        }
        cls.AUTHENTICATION_BACKENDS = [
            "twake.oidc.OIDCAuthenticationBackend"
            if backend == "core.authentication.backends.OIDCAuthenticationBackend"
            else backend
            for backend in cls.AUTHENTICATION_BACKENDS
        ]
        cls.MIDDLEWARE = [
            *cls.MIDDLEWARE,
            "twake.middleware.ActorMiddleware",
            "twake.middleware.DocumentMovedMiddleware",
        ]


class Development(TwakeMixin, impress.Development):
    """Development settings with the twake app."""


class Production(TwakeMixin, impress.Production):
    """Production settings with the twake app."""


class Test(TwakeMixin, impress.Test):
    """Test settings with the twake app."""
