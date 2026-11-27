"""Request-scoped pieces the signal receivers cannot see."""

import json
import re

from django.http import JsonResponse

from core import models

from . import events, views
from .models import Grant


class ActorMiddleware:
    """Remembers the request, so the receivers know who acts."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        events.set_request(request)
        try:
            return self.get_response(request)
        finally:
            events.set_request(None)


MOVE_PATH = re.compile(r"^/api/v[0-9.]+/documents/(?P<id>[0-9a-f-]{36})/move/?$")


class DocumentMovedMiddleware:
    """
    A move rewrites the paths in SQL and emits no signal: the event is written
    when `POST /documents/{id}/move/` has answered 200.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        match = MOVE_PATH.match(request.path)
        if request.method == "POST" and match and response.status_code == 200:
            document = models.Document.objects.filter(pk=match["id"]).first()
            if document is not None:
                events.emit_document(events.DOCUMENT_MOVED, document)
                # The move wiped the direct accesses of the document, the ones
                # the grants posed with them: given back, without a second email
                if Grant.objects.filter(document=document).exists():
                    views.recompute_accesses(document, notify=False)
        return response


LINK_PATH = re.compile(r"^/api/v[0-9.]+/documents/[0-9a-f-]{36}/link-configuration/?$")


class LinkReachMiddleware:
    """
    On the platform the link of a document is private or public, never open to
    every signed-in person of every organization: the dialog does not offer
    that reach, and the API refuses it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in ("PUT", "PATCH") and LINK_PATH.match(request.path):
            try:
                reach = json.loads(request.body or b"{}").get("link_reach")
            except ValueError, AttributeError:
                reach = None
            if reach == "authenticated":
                return JsonResponse(
                    {"link_reach": ["This reach is not available on the platform."]},
                    status=400,
                )
        return self.get_response(request)
