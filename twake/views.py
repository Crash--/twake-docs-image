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
from django.db.models.functions import Lower
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
from .entry_points import drive_members_of, drives_of, find_people
from .entry_points import entry_points as compute_entry_points
from .models import (
    DocumentGestures,
    Grant,
    GrantMember,
    ManagedAccess,
    WorkplaceUser,
)
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
        save_changed_grant(serializer)
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
        delete_grant(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)
    serializer = serializers.GrantSerializer(instance, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    save_changed_grant(serializer)
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


def person_of(user):
    """A person as the side service reads them, from a user loaded with
    `select_related("twake_workplace")`."""
    workplace = getattr(user, "twake_workplace", None)
    return {
        "sub": user.sub,
        "email": user.email,
        "workplace_fqdn": workplace.workplace_fqdn if workplace else "",
    }


def list_users(request):
    """Every person Docs knows with an instance, for the nightly pass."""
    users = (
        models.User.objects.filter(is_active=True, sub__isnull=False)
        .select_related("twake_workplace")
        .order_by("created_at", "id")
    )
    paginator = AdminPagination()
    page = paginator.paginate_queryset(users, request)
    return paginator.get_paginated_response([person_of(user) for user in page])


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
    document_ids = {doc.pk for entries, _ in found.values() for doc, *_ in entries}
    drives = drives_of(document_ids)
    member_emails, member_instances = drive_members_of(document_ids)
    workplace = dict(
        WorkplaceUser.objects.filter(user__in=users).values_list(
            "user_id", "workplace_fqdn"
        )
    )
    results = {}
    for identifier, user in people.items():
        entries, others = found[user.pk]
        email = (user.email or "").lower()
        fqdn = workplace.get(user.pk, "")
        results[identifier] = {
            "workplace_fqdn": fqdn,
            "entry_points": [
                {
                    "id": str(document.pk),
                    "title": document.title or "",
                    "role": role,
                    "deleted": deleted,
                    "drives": drives.get(document.pk, []),
                    "in_drive": email in member_emails[document.pk]
                    or bool(fqdn and fqdn in member_instances[document.pk]),
                }
                for document, role, deleted in entries
            ],
            "others": others,
        }
    return Response({"results": results})


GESTURE_DATES = {"title": "title_at", "trash": "trash_at", "move": "moved_at"}


@admin_route(["POST"])
def gestures(request, document_id):
    """Refuse a gesture of Drive older than the last one applied to the
    document, and record its date once done. The side service asks without
    `record`, does the gesture, then asks again with `record`. The second call
    assigns the date without the check: the gesture done through Docs has just
    dated the field to now, which would refuse its own record."""
    serializer = serializers.GestureSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    document = get_object_or_404(models.Document, pk=document_id)
    refused = Response({"applied": False}, status=status.HTTP_409_CONFLICT)
    with transaction.atomic():
        kept, _ = DocumentGestures.objects.select_for_update().get_or_create(
            document=document
        )
        if data["field"] == "copied_from":
            taken = (
                DocumentGestures.objects.filter(copied_from=data["key"])
                .exclude(pk=kept.pk)
                .exists()
            )
            if taken:
                return refused
            if data["record"]:
                kept.copied_from = data["key"]
                kept.save(update_fields=["copied_from"])
            return Response({"applied": True})
        column = GESTURE_DATES[data["field"]]
        if data["record"]:
            setattr(kept, column, data["as_of"])
            kept.save(update_fields=[column])
            return Response({"applied": True})
        date = getattr(kept, column)
        if date and data["as_of"] <= date:
            return refused
    return Response({"applied": True})


@admin_route(["POST"])
def copies(request):
    """The copy made for a file copied in Drive, by its key."""
    serializer = serializers.CopyKeySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    copy = DocumentGestures.objects.filter(
        copied_from=serializer.validated_data["key"]
    ).first()
    if copy is None:
        return Response({}, status=status.HTTP_404_NOT_FOUND)
    return Response({"id": str(copy.document_id)})


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
def admin_document(request, document_id):
    """A document with its owners, the people who reach it and its grants, in
    the trash too: the side service reads it to know who to write pointers to."""
    document = get_object_or_404(models.Document, pk=document_id)
    rows = models.DocumentAccess.objects.filter(
        document__path__in=document.get_self_and_ancestors_paths(),
        user__isnull=False,
    ).select_related("user", "user__twake_workplace")
    # A direct access that a grant also covers is not managed: it is a gesture
    managed_ids = set(
        ManagedAccess.objects.filter(access__in=rows, previous_role="").values_list(
            "access_id", flat=True
        )
    )
    owners, accessors = {}, {}
    for row in rows:
        if row.role == RoleChoices.OWNER:
            owners[row.user_id] = person_of(row.user)
        else:
            entry = accessors.setdefault(
                row.user_id, {**person_of(row.user), "managed": True}
            )
            # A person with a direct access on any level is not managed
            entry["managed"] = entry["managed"] and row.pk in managed_ids
    for user_id in owners:
        accessors.pop(user_id, None)
    tree = models.DocumentAccess.objects.filter(
        document__path__startswith=document.path, user__isnull=False
    ).select_related("user", "user__twake_workplace")
    parent = document.get_parent() if document.depth > 1 else None
    return Response(
        {
            "id": str(document.pk),
            "title": document.title or "",
            "parent_id": str(parent.pk) if parent else None,
            "root_id": str(document.get_root().pk),
            "deleted": document.ancestors_deleted_at is not None,
            "owners": list(owners.values()),
            "accessors": list(accessors.values()),
            "tree_accessors": list(
                {row.user_id: person_of(row.user) for row in tree}.values()
            ),
            "grants": serializers.GrantSerializer(
                Grant.objects.filter(document=document).order_by("created_at"),
                many=True,
            ).data,
        }
    )


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
        save_changed_grant(serializer)
        return Response(serializer.data)
    serializer.save(document=document)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@admin_route(["DELETE"])
def admin_delete_grant(request, document_id, grant_id):
    """Remove a shared drive grant."""
    instance = get_object_or_404(Grant, pk=grant_id, document_id=document_id)
    delete_grant(instance)
    return Response(status=status.HTTP_204_NO_CONTENT)


class ManagedAccessesSync:
    """One pass of `managed_accesses`, see the view."""

    def __init__(self, document, wanted, notify=True):
        self.document = document
        self.wanted = wanted
        self.notify = notify
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
        if self.sender and self.notify:
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


def wanted_accesses(document):
    """The role each person gets from the grants of a document: the strongest
    of their grants, `reader` when they are read-only members of a drive. The
    owners are left out, they have the document anyway."""
    owners = {
        email.lower()
        for email in models.DocumentAccess.objects.filter(
            document__path__in=document.get_self_and_ancestors_paths(),
            role=RoleChoices.OWNER,
            user__isnull=False,
        ).values_list("user__email", flat=True)
    }
    wanted = {}
    for member in GrantMember.objects.filter(
        grant__document=document, removed=False
    ).select_related("grant"):
        if member.email in owners:
            continue
        role = RoleChoices.READER if member.read_only else member.grant.role
        wanted[member.email] = RoleChoices.max(wanted.get(member.email), role)
    return wanted


def recompute_accesses(document, notify=True):
    """Bring the managed accesses of a document to what its grants give, and
    report what changed. `notify=False` poses them without the sharing email."""
    with transaction.atomic():
        # Two grants of a document are written by two requests: one at a time
        document = models.Document.objects.select_for_update().get(pk=document.pk)
        return ManagedAccessesSync(
            document, wanted_accesses(document), notify=notify
        ).run()


def count_members(grant):
    """Keep the count the modal shows on the line of the grant. A queryset
    update, so no `grant.updated` event comes back to the side service."""
    count = grant.members.filter(removed=False).count()
    Grant.objects.filter(pk=grant.pk).update(member_count=count)
    return count


def describe_changes(document, report, grant):
    """What a recompute did, in the form of the contract: the people whose access
    was posed or whose role changed, with their role, and the ones who lost it."""
    emails = {*report["created"], *report["updated"], *report["removed"]}
    users = {
        user.email.lower(): user
        for user in models.User.objects.annotate(lowered=Lower("email"))
        .filter(lowered__in=emails)
        .select_related("twake_workplace")
    }
    roles = dict(
        models.DocumentAccess.objects.filter(
            document=document, user__in=users.values()
        ).values_list("user_id", "role")
    )
    # The instance Docs knows, else the one the member came with
    fqdns = dict(
        grant.members.filter(email__in=emails).values_list("email", "workplace_fqdn")
    )

    def known(email):
        person = person_of(users[email])
        return {
            **person,
            "workplace_fqdn": person["workplace_fqdn"] or fqdns.get(email, ""),
        }

    return {
        "document_id": str(document.pk),
        "member_count": count_members(grant),
        "changed": [
            {**known(email), "role": roles.get(users[email].pk)}
            for email in dict.fromkeys([*report["created"], *report["updated"]])
            if email in users
        ],
        "removed": [known(email) for email in report["removed"] if email in users],
    }


def write_member(grant, rows, item, at, removed):
    """Write one member of a grant, unless a later change or a later full list
    already decided about them."""
    email = item["email"].lower()
    row = rows.get(email)
    # A person with no row yet is known from the full list the grant last had
    newest = row.at if row else grant.members_as_of
    if newest and newest > at:
        return
    values = {"at": at, "removed": removed}
    for field in ("sub", "workplace_fqdn"):
        if item.get(field):
            values[field] = item[field]
    if not removed:
        values["read_only"] = item["read_only"]
    if row is None:
        rows[email] = GrantMember.objects.create(grant=grant, email=email, **values)
        return
    for field, value in values.items():
        setattr(row, field, value)
    row.save(update_fields=list(values))


@admin_route(["PUT"])
def grant_members(request, grant_id):
    """
    The complete list of the members of a grant, read at `as_of`.

    The members of the list are written. A member who is not in it and whose row
    is older than `as_of` is marked removed: a row newer than `as_of` is a change
    that happened while the list was being read, and it stays. A list older than
    the last one given is refused (409).
    """
    serializer = serializers.GrantMembersSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    as_of = serializer.validated_data["as_of"]
    with transaction.atomic():
        grant = get_object_or_404(
            Grant.objects.select_for_update().select_related("document"), pk=grant_id
        )
        if grant.members_as_of and as_of < grant.members_as_of:
            return Response(
                {"as_of": ["A more recent list of members was already given."]},
                status=status.HTTP_409_CONFLICT,
            )
        rows = {row.email: row for row in grant.members.all()}
        listed = set()
        for item in serializer.validated_data["members"]:
            listed.add(item["email"].lower())
            write_member(grant, rows, item, as_of, removed=False)
        for email, row in rows.items():
            if email not in listed and not row.removed and row.at < as_of:
                row.removed = True
                row.at = as_of
                row.save(update_fields=["removed", "at"])
        Grant.objects.filter(pk=grant.pk).update(members_as_of=as_of)
        report = recompute_accesses(grant.document)
        body = describe_changes(grant.document, report, grant)
    return Response({**body, "unknown": report["unknown"]})


@admin_route(["POST"])
def source_members(request):
    """People added to or removed from a group or a drive, on every grant of
    that source at once. Each row keeps its date, so that a change older than
    the one already applied does nothing."""
    serializer = serializers.SourceMembersSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    documents = []
    with transaction.atomic():
        for grant in (
            Grant.objects.select_for_update()
            .select_related("document")
            .filter(source_kind=data["source_kind"], source_id=data["source_id"])
            .order_by("pk")
        ):
            rows = {row.email: row for row in grant.members.all()}
            for item in data.get("add", []):
                write_member(grant, rows, item, data["at"], removed=False)
            for item in data.get("remove", []):
                write_member(grant, rows, item, data["at"], removed=True)
            report = recompute_accesses(grant.document)
            documents.append(describe_changes(grant.document, report, grant))
    return Response({"documents": documents})


def save_changed_grant(serializer):
    """Save a grant; when its role changed, so do the accesses it gives."""
    old_role = serializer.instance.role
    grant = serializer.save()
    if grant.role != old_role:
        recompute_accesses(grant.document)


def delete_grant(grant):
    """Remove a grant and its members, and take back the accesses it gave."""
    document = grant.document
    with transaction.atomic():
        grant.delete()
        recompute_accesses(document)
