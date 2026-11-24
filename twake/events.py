"""The events of Docs, written to the outbox from the signals of its models."""

import threading

from django.db.models import signals
from django.dispatch import receiver

from core import models

from .models import Grant, ManagedAccess, Outbox

SOURCE = "twake://docs"

DOCUMENT_CREATED = "com.twake.docs.document.created.v1"
DOCUMENT_UPDATED = "com.twake.docs.document.updated.v1"
DOCUMENT_MOVED = "com.twake.docs.document.moved.v1"
DOCUMENT_TRASHED = "com.twake.docs.document.trashed.v1"
DOCUMENT_RESTORED = "com.twake.docs.document.restored.v1"
DOCUMENT_DELETED = "com.twake.docs.document.deleted.v1"
ACCESS_CREATED = "com.twake.docs.access.created.v1"
ACCESS_UPDATED = "com.twake.docs.access.updated.v1"
ACCESS_DELETED = "com.twake.docs.access.deleted.v1"
INVITATION_CREATED = "com.twake.docs.invitation.created.v1"
INVITATION_DELETED = "com.twake.docs.invitation.deleted.v1"
GRANT_CREATED = "com.twake.docs.grant.created.v1"
GRANT_UPDATED = "com.twake.docs.grant.updated.v1"
GRANT_DELETED = "com.twake.docs.grant.deleted.v1"

# The placement Drive wants for the pointer of the document about to be created,
# carried by its document.created event
_next_placement = threading.local()


def set_next_placement(placement):
    """Remember, for the current thread, where the next created document goes."""
    _next_placement.value = placement


_current = threading.local()


def set_request(request):
    """The request being served in this thread, None outside a request."""
    _current.request = request


def set_actor(actor):
    """For the tests and the commands: the actor outside a request."""
    _current.actor = actor or ""


def current_actor():
    """The `sub` of the user of the current request, empty outside a request."""
    request = getattr(_current, "request", None)
    if request is None:
        return getattr(_current, "actor", "")
    # DRF sets the authenticated user on the Django request as it authenticates
    user = getattr(request, "user", None)
    return getattr(user, "sub", "") or ""


def emit(event_type, data):
    """Write one event to the outbox, in the current transaction if any."""
    return Outbox.objects.create(type=event_type, actor=current_actor(), data=data)


def document_data(document):
    """What every document event carries."""
    parent = document.get_parent() if document.depth > 1 else None
    return {
        "id": str(document.pk),
        "title": document.title or "",
        "parent_id": str(parent.pk) if parent else None,
        "depth": document.depth,
        "path": document.path,
        "creator": document.creator.sub if document.creator_id else None,
        "link_reach": document.link_reach,
        "link_role": document.link_role,
        "deleted": document.deleted_at is not None
        or document.ancestors_deleted_at is not None,
    }


def emit_document(event_type, document, **extra):
    """Write a document event."""
    return emit(event_type, {**document_data(document), **extra})


@receiver(signals.post_save, sender=models.Document)
def document_saved(sender, instance, created, update_fields, **kwargs):
    # pylint: disable=unused-argument
    if created:
        placement = getattr(_next_placement, "value", None)
        _next_placement.value = None
        extra = {"placement": placement} if placement else {}
        emit_document(DOCUMENT_CREATED, instance, **extra)
        return
    fields = set(update_fields or ())
    if {"deleted_at", "ancestors_deleted_at"} & fields or (
        not fields and instance.deleted_at is not None
    ):
        if instance.deleted_at is not None:
            emit_document(DOCUMENT_TRASHED, instance)
        else:
            emit_document(DOCUMENT_RESTORED, instance)
        return
    emit_document(DOCUMENT_UPDATED, instance)


@receiver(signals.post_delete, sender=models.Document)
def document_deleted(sender, instance, **kwargs):
    # pylint: disable=unused-argument
    emit(DOCUMENT_DELETED, {"id": str(instance.pk)})


def access_data(access):
    """What every access event carries."""
    managed = ManagedAccess.objects.filter(access_id=access.pk).exists()
    return {
        "document_id": str(access.document_id),
        "access_id": str(access.pk),
        "sub": access.user.sub if access.user_id else None,
        "email": access.user.email if access.user_id else None,
        "team": access.team or None,
        "role": access.role,
        "managed": managed,
    }


@receiver(signals.post_save, sender=models.DocumentAccess)
def access_saved(sender, instance, created, **kwargs):
    # pylint: disable=unused-argument
    emit(ACCESS_CREATED if created else ACCESS_UPDATED, access_data(instance))


@receiver(signals.post_delete, sender=models.DocumentAccess)
def access_deleted(sender, instance, **kwargs):
    # pylint: disable=unused-argument
    emit(
        ACCESS_DELETED,
        {
            "document_id": str(instance.document_id),
            "access_id": str(instance.pk),
            "sub": instance.user.sub if instance.user_id else None,
            "email": instance.user.email if instance.user_id else None,
            "team": instance.team or None,
            "role": instance.role,
        },
    )


def mark_access_managed(access):
    """The creation event of an access the side service just posed: the
    receiver wrote it before the ManagedAccess row existed."""
    for row in Outbox.objects.filter(
        type=ACCESS_CREATED, sent_at__isnull=True, data__access_id=str(access.pk)
    ):
        row.data = {**row.data, "managed": True}
        row.save(update_fields=["data"])


def invitation_data(invitation):
    """What every invitation event carries."""
    return {
        "document_id": str(invitation.document_id),
        "invitation_id": str(invitation.pk),
        "email": invitation.email,
        "role": invitation.role,
    }


@receiver(signals.post_save, sender=models.Invitation)
def invitation_saved(sender, instance, created, **kwargs):
    # pylint: disable=unused-argument
    if created:
        emit(INVITATION_CREATED, invitation_data(instance))


@receiver(signals.post_delete, sender=models.Invitation)
def invitation_deleted(sender, instance, **kwargs):
    # pylint: disable=unused-argument
    emit(INVITATION_DELETED, invitation_data(instance))


def grant_data(grant):
    """What every grant event carries."""
    return {
        "document_id": str(grant.document_id),
        "grant_id": str(grant.pk),
        "source_kind": grant.source_kind,
        "source_id": grant.source_id,
        "name": grant.name,
        "role": grant.role,
    }


@receiver(signals.post_save, sender=Grant)
def grant_saved(sender, instance, created, **kwargs):
    # pylint: disable=unused-argument
    emit(GRANT_CREATED if created else GRANT_UPDATED, grant_data(instance))


@receiver(signals.post_delete, sender=Grant)
def grant_deleted(sender, instance, **kwargs):
    # pylint: disable=unused-argument
    emit(GRANT_DELETED, grant_data(instance))
