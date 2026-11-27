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


class WorkplaceUser(models.Model):
    """
    What the Workplace says about a person Docs knows: their instance and
    organization, and the date of the last settings applied, to ignore an older
    one that arrives late.
    """

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="twake_workplace"
    )
    workplace_fqdn = models.CharField(max_length=255, blank=True)
    organization_id = models.CharField(max_length=255, blank=True)
    settings_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "twake_workplace_user"

    def __str__(self):
        return f"{self.user_id} on {self.workplace_fqdn or 'an unknown instance'}"


def workplace_fqdn_of(user_id):
    """The instance of a person, empty when Docs does not know it."""
    if user_id is None:
        return ""
    return (
        WorkplaceUser.objects.filter(user_id=user_id)
        .values_list("workplace_fqdn", flat=True)
        .first()
        or ""
    )


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
    # The group of an organization, by its organization and its LDAP id: the
    # `source_id` is a hash the side service cannot reverse
    organization_id = models.CharField(max_length=255, null=True, blank=True)
    external_id = models.CharField(max_length=255, null=True, blank=True)
    # The instance that owns the shared drive
    owner_instance = models.CharField(max_length=255, null=True, blank=True)
    # The date of the member list the grant was last given in full, to ignore
    # an older one
    members_as_of = models.DateTimeField(null=True, blank=True)
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


class GrantMember(models.Model):
    """
    A person a grant gives access to, as the side service last told Docs. The
    date `at` and the `removed` mark are kept so that a change that arrives late,
    or in the wrong order, does nothing.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="members")
    # In lowercase
    email = models.CharField(max_length=255)
    sub = models.CharField(max_length=255, blank=True)
    workplace_fqdn = models.CharField(max_length=255, blank=True)
    read_only = models.BooleanField(default=False)
    at = models.DateTimeField()
    removed = models.BooleanField(default=False)

    class Meta:
        db_table = "twake_grant_member"
        constraints = [
            models.UniqueConstraint(
                fields=["grant", "email"], name="twake_grant_member_unique_email"
            )
        ]

    def __str__(self):
        return f"{self.email} in {self.grant_id}{' (removed)' if self.removed else ''}"


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
