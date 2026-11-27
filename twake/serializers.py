"""Serializers of the twake routes."""

import datetime

from django.conf import settings

from rest_framework import serializers

from core.choices import RoleChoices

from .models import Grant, SourceKind


class MillisecondDateTimeField(serializers.DateTimeField):
    """RFC 3339 in UTC to the millisecond, the form the side service compares."""

    def to_representation(self, value):
        if value is None:
            return None
        return (
            value.astimezone(datetime.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )


class PlacementSerializer(serializers.Serializer):
    """Where Drive wants the pointer of a document created from it."""

    instance = serializers.CharField(max_length=255)
    dir_id = serializers.CharField(max_length=255)


class CreateDocumentSerializer(serializers.Serializer):
    """`POST /api/v1.0/twake/documents/`."""

    title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    placement = PlacementSerializer(required=False)


class GrantSerializer(serializers.ModelSerializer):
    """A group or shared drive sharing of a document."""

    members_as_of = MillisecondDateTimeField(read_only=True)

    class Meta:
        model = Grant
        fields = [
            "id",
            "document",
            "source_kind",
            "source_id",
            "name",
            "member_count",
            "role",
            "organization_id",
            "external_id",
            "owner_instance",
            "members_as_of",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "document", "created_at", "updated_at"]

    def validate_source_kind(self, value):
        if (
            self.instance is None
            and value != SourceKind.GROUP
            and not self.context.get("admin")
        ):
            raise serializers.ValidationError(
                "Only groups are shared from the modal; shared drives follow the "
                "pointer."
            )
        return value

    def validate_role(self, value):
        if value == RoleChoices.OWNER:
            raise serializers.ValidationError("A group cannot own a document.")
        return value


class ShareByEmailSerializer(serializers.Serializer):
    """`POST /api/v1.0/twake/documents/{id}/accesses/`: a recipient by email."""

    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=RoleChoices.choices)


class EnsureUserSerializer(serializers.Serializer):
    """`POST /twake/users/`: a user known from the Workplace, before any login."""

    sub = serializers.CharField(max_length=255)
    email = serializers.EmailField()
    full_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    language = serializers.ChoiceField(
        choices=[lang[0] for lang in settings.LANGUAGES], required=False
    )
    workplace_fqdn = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    organization_id = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )


class UserSettingsSerializer(serializers.Serializer):
    """`PUT /twake/users/settings/`: the common settings of the Workplace."""

    email = serializers.EmailField()
    updated_at = serializers.DateTimeField(required=False)
    language = serializers.ChoiceField(
        choices=[lang[0] for lang in settings.LANGUAGES],
        required=False,
        allow_null=True,
    )
    timezone = serializers.CharField(max_length=64, required=False, allow_null=True)


class EntryPointsSerializer(serializers.Serializer):
    """`POST /twake/entry-points/`: up to a batch of people, a tree or not."""

    people = serializers.ListField(
        child=serializers.CharField(max_length=255), min_length=1, max_length=200
    )
    within = serializers.UUIDField(required=False)


class MemberSerializer(serializers.Serializer):
    """A person of a group or a drive, named by email."""

    email = serializers.EmailField()
    sub = serializers.CharField(max_length=255, required=False, allow_blank=True)
    workplace_fqdn = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    read_only = serializers.BooleanField(required=False, default=False)


class GrantMembersSerializer(serializers.Serializer):
    """`PUT /twake/grants/{id}/members/`: the complete list at a date."""

    as_of = serializers.DateTimeField()
    members = MemberSerializer(many=True)


class SourceMembersSerializer(serializers.Serializer):
    """`POST /twake/grants/members/`: members added or removed at a date, on
    every grant of a source."""

    source_kind = serializers.ChoiceField(choices=SourceKind.choices)
    source_id = serializers.CharField(max_length=255)
    at = serializers.DateTimeField()
    add = MemberSerializer(many=True, required=False)
    remove = MemberSerializer(many=True, required=False)


class ManagedAccessItemSerializer(serializers.Serializer):
    """One member a grant gives access to."""

    email = serializers.EmailField()
    role = serializers.ChoiceField(
        choices=[r for r in RoleChoices.values if r != RoleChoices.OWNER]
    )


class ManagedAccessesSerializer(serializers.Serializer):
    """`PUT /twake/documents/{id}/managed-accesses/`: the wanted state."""

    accesses = ManagedAccessItemSerializer(many=True)
