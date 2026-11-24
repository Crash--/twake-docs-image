# Docs for Twake Workplace: the upstream backend image plus the twake app.
# The upstream code is not modified; DJANGO_SETTINGS_MODULE selects the
# settings that load the app.
ARG DOCS_VERSION=v6.0.4
FROM lasuite/impress-backend:${DOCS_VERSION}

COPY twake_settings.py /app/twake_settings.py
COPY twake /app/twake

ENV DJANGO_SETTINGS_MODULE=twake_settings
