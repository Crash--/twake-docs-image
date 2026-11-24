"""Request-scoped pieces the signal receivers cannot see."""

import re

from core import models

from . import events


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
        return response
