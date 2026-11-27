"""Routes of the twake app.

Two families: the routes of the Docs front, with the session of the user, and
the admin routes of the side service, with the server-to-server token.
"""

import base64
import json
import time
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.urls import reverse

from rest_framework import exceptions, status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core import models
from core.authentication import ServerToServerAuthentication
from core.choices import PRIVILEGED_ROLES, RoleChoices
from core.utils.treebeard import create_tree_node_with_retry

from . import events, serializers
from .entry_points import drives_of, find_people
from .entry_points import entry_points as compute_entry_points
from .models import Grant, ManagedAccess, WorkplaceUser
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


ID_TOKEN_MARGIN_SECONDS = 5 * 60


def id_token_expiry(id_token):
    """The `exp` claim of a JWT, read without checking the signature: the
    token is the one the backend itself got from the SSO. None if unreadable."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
        return int(claims["exp"])
    except AttributeError, IndexError, KeyError, TypeError, ValueError:
        return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def session(request):
    """What the Twake bar and SDK of the front need: the SSO id token and the
    Workplace address of the user.

    The SSO gives id tokens of one hour and nobody renews them, while the
    session of Docs lasts 12 hours. Like Mail and Calendar, which go through
    the SSO again at each load, an id token about to expire is not given: the
    answer is a 401 with the URL of a silent login (`prompt=none`) that comes
    back to the page, `next`, with a new id token.
    """
    id_token = request.session.get("oidc_id_token")
    workplace_fqdn = request.session.get(WORKPLACE_FQDN_SESSION_KEY)
    expiry = id_token_expiry(id_token)
    if workplace_fqdn and (
        expiry is None or expiry - time.time() < ID_TOKEN_MARGIN_SECONDS
    ):
        query = urlencode(
            {"silent": "true", "next": request.query_params.get("next") or "/"}
        )
        return Response(
            {
                "detail": "The id token of the session has expired.",
                "refresh_url": f"{reverse('oidc_authentication_init')}?{query}",
            },
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return Response(
        {
            "sub": request.user.sub,
            "id_token": id_token,
            "workplace_fqdn": workplace_fqdn,
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


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def share_by_email(request, document_id):
    """Share a document with a person named by email, as the suggestions of the
    Workplace give them: an access when Docs knows the person, an invitation
    otherwise, with the sharing email of Docs in both cases."""
    document = document_or_404(document_id)
    if not can_manage(document, request.user):
        raise exceptions.PermissionDenied()
    serializer = serializers.ShareByEmailSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data["email"]
    role = serializer.validated_data["role"]
    if (
        role == RoleChoices.OWNER
        and document.get_role(request.user) != RoleChoices.OWNER
    ):
        raise exceptions.PermissionDenied("Only owners can assign owners.")
    user = models.User.objects.filter(email__iexact=email).first()
    if user is not None:
        access = models.DocumentAccess.objects.filter(
            document=document, user=user
        ).first()
        if access is None:
            access = models.DocumentAccess.objects.create(
                document=document, user=user, role=role
            )
            document.send_invitation_email(
                user.email,
                role,
                request.user,
                user.language or request.user.language or settings.LANGUAGE_CODE,
            )
            created = True
        else:
            # The same person again: one line, the new role (story P13). Given
            # by hand, the access is direct from now on, whatever the grants
            # that posed it (stories G4, S7).
            ManagedAccess.objects.filter(access=access).delete()
            if access.role != role:
                access.role = role
                access.save(update_fields=["role", "updated_at"])
            created = False
        return Response(
            {"kind": "access", "id": str(access.pk), "role": access.role},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
    invitation, created = models.Invitation.objects.get_or_create(
        email=email, document=document, defaults={"role": role, "issuer": request.user}
    )
    if created:
        document.send_invitation_email(
            email, role, request.user, request.user.language or settings.LANGUAGE_CODE
        )
    elif invitation.role != role:
        invitation.role = role
        invitation.save(update_fields=["role", "updated_at"])
    return Response(
        {"kind": "invitation", "id": str(invitation.pk), "role": invitation.role},
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def managed_access_ids(request, document_id):
    """The accesses the grants of a document posed, without a direct role: the
    share dialog shows the group or the drive, not one line per member."""
    document = document_or_404(document_id)
    if document.get_role(request.user) is None and document.link_reach == "restricted":
        raise exceptions.PermissionDenied()
    ids = ManagedAccess.objects.filter(
        access__document=document, previous_role=""
    ).values_list("access_id", flat=True)
    return Response({"access_ids": [str(i) for i in ids]})


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


class AdminPagination(PageNumberPagination):
    """Pages of at most 200, the size of a batch of the side service."""

    page_size = 200
    page_size_query_param = "page_size"
    max_page_size = 200


def list_users(request):
    """Every person Docs knows with an instance, for the nightly pass."""
    users = (
        models.User.objects.filter(is_active=True, sub__isnull=False)
        .select_related("twake_workplace")
        .order_by("created_at", "id")
    )
    paginator = AdminPagination()
    page = paginator.paginate_queryset(users, request)
    return paginator.get_paginated_response(
        [
            {
                "sub": user.sub,
                "email": user.email,
                "workplace_fqdn": workplace.workplace_fqdn if workplace else "",
            }
            for user in page
            for workplace in [getattr(user, "twake_workplace", None)]
        ]
    )


@admin_route(["GET", "POST"])
def ensure_user(request):
    """Create a user ahead of their first login, and convert their invitations
    into accesses, as Docs does at the first login (`User.save`). A GET lists
    the users."""
    if request.method == "GET":
        return list_users(request)
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
    workplace = {
        field: data[field]
        for field in ("workplace_fqdn", "organization_id")
        if field in data
    }
    if workplace:
        WorkplaceUser.objects.update_or_create(user=user, defaults=workplace)
    return Response(
        {"id": str(user.pk), "sub": user.sub, "created": created},
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@admin_route(["POST"])
def entry_points(request):
    """The entry points of a batch of people, what `GET /documents/` of Docs
    gives each of them, in a few queries instead of one request per person."""
    serializer = serializers.EntryPointsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    within = None
    if "within" in serializer.validated_data:
        within = get_object_or_404(
            models.Document, pk=serializer.validated_data["within"]
        )
    people = find_people(serializer.validated_data["people"])
    users = list({user.pk: user for user in people.values()}.values())
    found = compute_entry_points(users, within)
    drives = drives_of({doc.pk for entries, _ in found.values() for doc, *_ in entries})
    workplace = dict(
        WorkplaceUser.objects.filter(user__in=users).values_list(
            "user_id", "workplace_fqdn"
        )
    )
    results = {}
    for identifier, user in people.items():
        entries, others = found[user.pk]
        results[identifier] = {
            "workplace_fqdn": workplace.get(user.pk, ""),
            "entry_points": [
                {
                    "id": str(document.pk),
                    "title": document.title or "",
                    "role": role,
                    "deleted": deleted,
                    "drives": drives.get(document.pk, []),
                }
                for document, role, deleted in entries
            ],
            "others": others,
        }
    return Response({"results": results})


@admin_route(["PUT"])
def user_settings(request):
    """Apply the language and timezone of the Workplace settings to a user."""
    serializer = serializers.UserSettingsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    user = models.User.objects.filter(email__iexact=data["email"]).first()
    if user is None:
        return Response({"applied": False}, status=status.HTTP_404_NOT_FOUND)
    updated_at = data.get("updated_at")
    # The date and the settings it guards are written together
    with transaction.atomic():
        workplace, _ = WorkplaceUser.objects.select_for_update().get_or_create(
            user=user
        )
        if updated_at and workplace.settings_at and updated_at < workplace.settings_at:
            return Response({"applied": False})
        if updated_at:
            workplace.settings_at = updated_at
            workplace.save(update_fields=["settings_at"])
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


@admin_route(["GET"])
def admin_grants(request):
    """The grants of a source, or all of them for the nightly pass."""
    queryset = Grant.objects.order_by("created_at", "id")
    for field in ("source_kind", "source_id"):
        if field in request.query_params:
            queryset = queryset.filter(**{field: request.query_params[field]})
    paginator = AdminPagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response(
        serializers.GrantSerializer(page, many=True).data
    )


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
