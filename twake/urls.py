"""URLs of the Twake image: the upstream ones plus the twake routes."""

from django.conf import settings
from django.urls import path

from impress.urls import urlpatterns as impress_urlpatterns

from . import views

API = f"api/{settings.API_VERSION}/twake/"

urlpatterns = [
    # Front of Docs, session of the user
    path(f"{API}session/", views.session, name="twake-session"),
    path(f"{API}documents/", views.create_document, name="twake-create-document"),
    path(
        f"{API}documents/<uuid:document_id>/accesses/",
        views.share_by_email,
        name="twake-share-by-email",
    ),
    path(
        f"{API}documents/<uuid:document_id>/accesses/managed/",
        views.managed_access_ids,
        name="twake-managed-access-ids",
    ),
    path(
        f"{API}documents/<uuid:document_id>/grants/",
        views.grants,
        name="twake-grants",
    ),
    path(
        f"{API}documents/<uuid:document_id>/grants/<uuid:grant_id>/",
        views.grant,
        name="twake-grant",
    ),
    # Side service, server-to-server token, not exposed by the front proxy
    path("twake/users/", views.ensure_user, name="twake-admin-users"),
    path("twake/users/settings/", views.user_settings, name="twake-admin-settings"),
    path("twake/entry-points/", views.entry_points, name="twake-admin-entry-points"),
    path(
        "twake/documents/<uuid:document_id>/",
        views.admin_document,
        name="twake-admin-document",
    ),
    path(
        "twake/documents/<uuid:document_id>/managed-accesses/",
        views.managed_accesses,
        name="twake-admin-managed-accesses",
    ),
    path(
        "twake/documents/<uuid:document_id>/grants/",
        views.admin_create_grant,
        name="twake-admin-create-grant",
    ),
    path(
        "twake/documents/<uuid:document_id>/grants/<uuid:grant_id>/",
        views.admin_delete_grant,
        name="twake-admin-delete-grant",
    ),
    path("twake/grants/", views.admin_grants, name="twake-admin-grants"),
    path("twake/grants/<uuid:grant_id>/", views.admin_grant, name="twake-admin-grant"),
    *impress_urlpatterns,
]
