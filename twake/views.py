"""Routes of the twake app.

Two families: the routes of the Docs front, with the session of the user, and
the admin routes of the side service, with the server-to-server token.
"""

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404

from rest_framework import exceptions, status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core import models
from core.authentication import ServerToServerAuthentication
from core.choices import PRIVILEGED_ROLES, RoleChoices
from core.utils.treebeard import create_tree_node_with_retry

from . import events, serializers
from .models import Grant, ManagedAccess
from .oidc import WORKPLACE_FQDN_SESSION_KEY


def document_or_404(document_id):
    """A document that is not in the trash."""
    return get_object_or_404(
        models.Document, pk=document_id, ancestors_deleted_at__isnull=True
    )


def can_manage(document, user):
    """Who may share: administrators and owners, ancestors included."""
    return document.get_role(user) in PRIVILEGED_ROLES


# Front routes, session of the user


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def session(request):
    """What the Twake bar and SDK of the front need: the SSO id token and the
    Workplace address of the user."""
    return Response(
        {
            "sub": request.user.sub,
            "id_token": request.session.get("oidc_id_token"),
            "workplace_fqdn": request.session.get(WORKPLACE_FQDN_SESSION_KEY),
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_document(request):
    """Create a root document, with the placement of its pointer when Drive asks
    for it. The side service writes the pointer from the event."""
    serializer = serializers.CreateDocumentSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    placement = serializer.validated_data.get("placement")
    with transaction.atomic():
        events.set_next_placement(placement)
        try:
            document = create_tree_node_with_retry(
                lambda: models.Document.add_root(
                    creator=request.user,
                    title=serializer.validated_data.get("title") or None,
                )
            )
        finally:
            events.set_next_placement(None)
        models.DocumentAccess.objects.create(
            document=document, user=request.user, role=RoleChoices.OWNER
        )
    return Response({"id": str(document.pk)}, status=status.HTTP_201_CREATED)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def grants(request, document_id):
    """List the grants of a document, or share it with a group."""
    document = document_or_404(document_id)
    role = document.get_role(request.user)
    if request.method == "GET":
        if role is None and document.link_reach == "restricted":
            raise exceptions.PermissionDenied()
        queryset = Grant.objects.filter(document=document).order_by("created_at")
        return Response(serializers.GrantSerializer(queryset, many=True).data)
    if role not in PRIVILEGED_ROLES:
        raise exceptions.PermissionDenied()
    serializer = serializers.GrantSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    existing = Grant.objects.filter(
        document=document,
        source_kind=serializer.validated_data["source_kind"],
        source_id=serializer.validated_data["source_id"],
    ).first()
    if existing:
        serializer = serializers.GrantSerializer(
            existing, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
    serializer.save(document=document, created_by=request.user)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def grant(request, document_id, grant_id):
    """Change the role of a grant, or remove it."""
    document = document_or_404(document_id)
    if not can_manage(document, request.user):
        raise exceptions.PermissionDenied()
    instance = get_object_or_404(Grant, pk=grant_id, document=document)
    if request.method == "DELETE":
        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    serializer = serializers.GrantSerializer(instance, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)


# Admin routes, server-to-server token


def admin_route(methods):
    """The decorators of an admin route."""

    def decorate(view):
        view = permission_classes([])(view)
        view = authentication_classes([ServerToServerAuthentication])(view)
        return api_view(methods)(view)

    return decorate


@admin_route(["POST"])
def ensure_user(request):
    """Create a user ahead of their first login, and convert their invitations
    into accesses, as Docs does at the first login (`User.save`)."""
    serializer = serializers.EnsureUserSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    user = models.User.objects.filter(sub=data["sub"]).first()
    created = False
    if user is None:
        user = models.User.objects.filter(email__iexact=data["email"]).first()
        if user is not None and user.sub and user.sub != data["sub"]:
            return Response(
                {"email": ["This email belongs to another user."]},
                status=status.HTTP_409_CONFLICT,
            )
    if user is None:
        user = models.User(
            sub=data["sub"],
            email=data["email"],
            full_name=data.get("full_name") or None,
            language=data.get("language") or settings.LANGUAGE_CODE,
        )
        user.set_unusable_password()
        user.save()
        created = True
        # The conversion of the invitations is a bulk_create: no signal
        for access in models.DocumentAccess.objects.filter(user=user):
            events.emit(events.ACCESS_CREATED, events.access_data(access))
    return Response(
        {"id": str(user.pk), "sub": user.sub, "created": created},
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@admin_route(["PUT"])
def user_settings(request):
    """Apply the language and timezone of the Workplace settings to a user."""
    serializer = serializers.UserSettingsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    user = models.User.objects.filter(email__iexact=data["email"]).first()
    if user is None:
        return Response({"applied": False}, status=status.HTTP_404_NOT_FOUND)
    fields = []
    if data.get("language"):
        user.language = data["language"]
        fields.append("language")
    if data.get("timezone"):
        user.timezone = data["timezone"]
        fields.append("timezone")
    if fields:
        user.save(update_fields=fields)
    return Response({"applied": bool(fields), "fields": fields})


@admin_route(["GET", "PATCH"])
def admin_grant(request, grant_id):
    """Read a grant, or update what the side service knows about it: the number
    of members."""
    instance = get_object_or_404(Grant, pk=grant_id)
    if request.method == "PATCH":
        if "member_count" in request.data:
            instance.member_count = request.data["member_count"]
            instance.save(update_fields=["member_count", "updated_at"])
    return Response(serializers.GrantSerializer(instance).data)


@admin_route(["POST"])
def admin_create_grant(request, document_id):
    """A shared drive grant, posed by the side service when a pointer enters a
    shared drive. The side service is the only one who knows the drive."""
    document = document_or_404(document_id)
    data = {**request.data, "source_kind": "shared_drive"}
    existing = Grant.objects.filter(
        document=document, source_kind="shared_drive", source_id=data.get("source_id")
    ).first()
    serializer = serializers.GrantSerializer(
        existing, data=data, partial=bool(existing), context={"admin": True}
    )
    serializer.is_valid(raise_exception=True)
    if existing:
        serializer.save()
        return Response(serializer.data)
    serializer.save(document=document)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@admin_route(["DELETE"])
def admin_delete_grant(request, document_id, grant_id):
    """Remove a shared drive grant."""
    instance = get_object_or_404(Grant, pk=grant_id, document_id=document_id)
    instance.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


class ManagedAccessesSync:
    """One pass of `managed_accesses`, see the view."""

    def __init__(self, document, wanted):
        self.document = document
        self.wanted = wanted
        self.sender = (
            document.creator
            or models.User.objects.filter(
                accesses__document=document, accesses__role=RoleChoices.OWNER
            ).first()
        )
        self.report = {
            "created": [],
            "updated": [],
            "removed": [],
            "invited": [],
            "unknown": [],
        }
        self.managed = {
            m.access.user.email.lower(): m
            for m in ManagedAccess.objects.filter(
                access__document=document, access__user__isnull=False
            ).select_related("access__user")
        }

    def run(self):
        """Apply the wanted roles, then take back what no grant wants anymore."""
        for email, role in self.wanted.items():
            user = models.User.objects.filter(email__iexact=email).first()
            if user is None:
                self.invite(email, role)
            else:
                self.give(user, role)
        for email, entry in self.managed.items():
            if email not in self.wanted:
                self.take_back(email, entry)
        return self.report

    def email_sharing(self, email, role, language):
        if self.sender:
            self.document.send_invitation_email(email, role, self.sender, language)

    def invite(self, email, role):
        """No Docs account yet: an invitation, converted at the account creation."""
        self.report["unknown"].append(email)
        invitation, was_created = models.Invitation.objects.get_or_create(
            email=email, document=self.document, defaults={"role": role}
        )
        if was_created:
            self.report["invited"].append(email)
            self.email_sharing(email, role, self.sender and self.sender.language)
        elif invitation.role != role:
            invitation.role = role
            invitation.save(update_fields=["role", "updated_at"])

    def give(self, user, role):
        """An access, or the strongest of the grant role and the direct one."""
        email = user.email.lower()
        access = models.DocumentAccess.objects.filter(
            document=self.document, user=user
        ).first()
        if access is None:
            access = models.DocumentAccess.objects.create(
                document=self.document, user=user, role=role
            )
            ManagedAccess.objects.create(access=access, previous_role="")
            events.mark_access_managed(access)
            self.report["created"].append(email)
            language = user.language or (self.sender and self.sender.language)
            self.email_sharing(user.email, role, language or settings.LANGUAGE_CODE)
            return
        entry = self.managed.get(email)
        if entry is None:
            # A direct access: keep the strongest, remember the direct one
            entry = ManagedAccess.objects.create(
                access=access, previous_role=access.role
            )
        strongest = RoleChoices.max(role, entry.previous_role or role)
        if access.role != strongest:
            access.role = strongest
            access.save(update_fields=["role", "updated_at"])
            self.report["updated"].append(email)

    def take_back(self, email, entry):
        """No grant anymore: the direct role comes back, or the access goes."""
        access = entry.access
        if entry.previous_role:
            if access.role != entry.previous_role:
                access.role = entry.previous_role
                access.save(update_fields=["role", "updated_at"])
                self.report["updated"].append(email)
            entry.delete()
        else:
            access.delete()
            self.report["removed"].append(email)


@admin_route(["PUT"])
def managed_accesses(request, document_id):
    """
    Make the accesses of a document match what its grants give, person by person:
    the wanted role of each member, computed by the side service from every grant.

    - a member without access gets one, and the sharing email of Docs;
    - a member with a direct access keeps the strongest role, their direct role
      is kept aside and given back when the grants stop concerning them;
    - a managed access that no grant wants anymore goes back to the direct
      role, or is removed.
    """
    document = document_or_404(document_id)
    serializer = serializers.ManagedAccessesSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    wanted = {
        item["email"].lower(): item["role"]
        for item in serializer.validated_data["accesses"]
    }
    with transaction.atomic():
        report = ManagedAccessesSync(document, wanted).run()
    return Response(report)
