"""The look of Twake, given to the front by the configuration of Docs."""

from django.core.cache import cache

import pytest

pytestmark = pytest.mark.django_db


def test_config_gives_the_twake_theme(client):
    """The front loads the sheet of the fork and the Twake icon and name."""
    cache.clear()

    config = client.get("/api/v1.0/config/").json()

    assert config["FRONTEND_CSS_URL"] == "/twake-theme/theme.css"
    theme = config["theme_customization"]
    assert theme["header"]["icon"]["src"] == "/twake-theme/docs-icon.svg"
    assert theme["favicon"]["light"]["href"] == "/twake-theme/docs-icon.svg"
    assert theme["translations"]["fr"]["translation"]["Docs"] == "Twake Docs"
