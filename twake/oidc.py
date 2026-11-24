"""OIDC backend keeping the Workplace address of the user in the session."""

from core.authentication.backends import (
    OIDCAuthenticationBackend as DocsOIDCAuthenticationBackend,
)

WORKPLACE_FQDN_SESSION_KEY = "twake_workplace_fqdn"
WORKPLACE_FQDN_CLAIM = "workplaceFqdn"


class OIDCAuthenticationBackend(DocsOIDCAuthenticationBackend):
    """
    The `workplaceFqdn` claim names the user's Twake Workplace instance. Docs'
    User model has no field for it: it is kept in the session, next to the id
    token, for the Twake bar and SDK of the front.
    """

    def get_userinfo(self, access_token, id_token, payload):
        user_info = super().get_userinfo(access_token, id_token, payload)
        self._twake_workplace_fqdn = user_info.get(WORKPLACE_FQDN_CLAIM)
        return user_info

    def authenticate(self, request, **kwargs):
        user = super().authenticate(request, **kwargs)
        if user is not None and request is not None:
            fqdn = getattr(self, "_twake_workplace_fqdn", None)
            if fqdn:
                request.session[WORKPLACE_FQDN_SESSION_KEY] = fqdn
            else:
                request.session.pop(WORKPLACE_FQDN_SESSION_KEY, None)
        return user
