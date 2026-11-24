"""Authentication of the side service acting on behalf of a user."""

from django.conf import settings

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from core import models


class OnBehalfAuthentication(BaseAuthentication):
    """
    A server-to-server token plus an `X-User-Sub` header: the request runs with
    the rights of that user, through the permissions Docs already has.

    Without the header the class steps aside, so the session authentication of
    Docs applies as before.
    """

    def authenticate(self, request):
        sub = request.headers.get("X-User-Sub")
        auth = request.headers.get("Authorization", "").split(" ")
        if not sub or len(auth) != 2 or auth[0] != "Bearer":
            return None
        if auth[1] not in settings.SERVER_TO_SERVER_API_TOKENS:
            raise AuthenticationFailed("Invalid server-to-server token.")
        try:
            return models.User.objects.get(sub=sub, is_active=True), None
        except models.User.DoesNotExist as err:
            raise AuthenticationFailed("Unknown user.") from err

    def authenticate_header(self, request):
        return "Bearer realm='twake'"
