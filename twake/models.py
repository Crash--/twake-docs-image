"""Tables of the twake app, in the database of Docs."""

import uuid

from django.db import models

from core.choices import RoleChoices
from core.models import Document, DocumentAccess, User


class Outbox(models.Model):
    """
    An event of Docs waiting to be published on RabbitMQ. Written in the
    transaction of the change it describes, published by the relay command.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=100)
    time = models.DateTimeField(auto_now_add=True)
    actor = models.CharField(max_length=255, blank=True)
    data = models.JSONField()
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "twake_outbox"
        ordering = ("time", "id")
        indexes = [models.Index(fields=["sent_at", "time"])]

    def __str__(self):
        return f"{self.type} {self.id}"


class SourceKind(models.TextChoices):
    """Where a grant comes from."""

    GROUP = "group"
    SHARED_DRIVE = "shared_drive"


class Grant(models.Model):
    """
    A document shared with a group of the organization, or placed in a shared
    drive. Docs shows it as one line; the side service turns it into one access
    per member.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="twake_grants"
    )
    source_kind = models.CharField(max_length=20, choices=SourceKind.choices)
    source_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255, blank=True)
    member_count = models.PositiveIntegerField(null=True, blank=True)
    role = models.CharField(max_length=20, choices=RoleChoices.choices)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "twake_grant"
        constraints = [
            models.UniqueConstraint(
                fields=["document", "source_kind", "source_id"],
                name="twake_grant_unique_source",
            )
        ]

    def __str__(self):
        return (
            f"{self.source_kind} {self.source_id} is {self.role} in {self.document_id}"
        )


class ManagedAccess(models.Model):
    """
    An access the side service posed because of grants, with the direct role
    the person had before, given back when no grant concerns them anymore.
    """

    access = models.OneToOneField(
        DocumentAccess, on_delete=models.CASCADE, related_name="twake_managed"
    )
    previous_role = models.CharField(
        max_length=20, choices=RoleChoices.choices, blank=True
    )

    class Meta:
        db_table = "twake_managed_access"

    def __str__(self):
        return f"managed {self.access_id} (was {self.previous_role or 'nothing'})"
