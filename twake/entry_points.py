"""Entry points of many people at once, for the side service."""

from collections import defaultdict

from django.db.models import Q
from django.db.models.functions import Lower

from core import models
from core.api.utils import filter_root_paths
from core.choices import RoleChoices

from .models import Grant, SourceKind


def find_people(identifiers):
    """The active users named by `sub` or email, by identifier. A person Docs
    does not know is left out."""
    wanted = set(identifiers)
    by_sub = {
        user.sub: user
        for user in models.User.objects.filter(sub__in=wanted, is_active=True)
    }
    emails = {i.lower() for i in wanted - by_sub.keys()}
    by_email = {
        user.email.lower(): user
        for user in models.User.objects.annotate(lowered=Lower("email")).filter(
            lowered__in=emails, is_active=True
        )
    }
    found = {}
    for identifier in wanted:
        if identifier in by_sub:
            found[identifier] = by_sub[identifier]
        elif identifier.lower() in by_email:
            found[identifier.lower()] = by_email[identifier.lower()]
    return found


def _within_filter(within, prefix):
    """The documents of the tree of `within`, and the ancestors that can give
    access to it: nothing else changes an entry point of that tree."""
    paths = [
        within.path[:end]
        for end in range(within.steplen, len(within.path), within.steplen)
    ]
    return Q(**{f"{prefix}path__startswith": within.path}) | Q(
        **{f"{prefix}path__in": paths}
    )


def _link_traces(users, within):
    """The documents a person visited by a link, the other way the list of Docs
    shows a document without access."""
    traces = models.LinkTrace.objects.filter(
        user__in=users, document__ancestors_deleted_at__isnull=True
    ).exclude(document__link_reach=models.LinkReachChoices.RESTRICTED)
    if within is not None:
        traces = traces.filter(_within_filter(within, "document__"))
    return traces.values_list("user_id", "document__path")


def entry_points(users, within=None):
    """
    What `GET /documents/` of Docs gives each user: the highest documents they
    can reach, by an access or a visited link, out of the trash. The trashed
    documents the person owns come with `deleted` set: their pointer stays in the
    trash of Drive.

    Returns, per user id, a list of `(document, role, deleted)` and the ids of
    the documents of the tree of `within` that are not an entry point.
    """
    accesses = models.DocumentAccess.objects.filter(
        user__in=users, document__ancestors_deleted_at__isnull=True
    )
    trashed = models.DocumentAccess.objects.filter(
        user__in=users,
        role=RoleChoices.OWNER,
        document__deleted_at__isnull=False,
        document__deleted_at__gte=models.get_trashbin_cutoff(),
    )
    if within is not None:
        accesses = accesses.filter(_within_filter(within, "document__"))
        trashed = trashed.filter(_within_filter(within, "document__"))

    roles = defaultdict(list)
    candidates = defaultdict(set)
    for user_id, path, role in accesses.values_list(
        "user_id", "document__path", "role"
    ):
        roles[user_id].append((path, role))
        candidates[user_id].add(path)
    for user_id, path in _link_traces(users, within):
        candidates[user_id].add(path)

    live = {}
    for user_id, paths in candidates.items():
        live[user_id] = set(filter_root_paths(sorted(paths)))
    deleted = defaultdict(set)
    for user_id, path in trashed.values_list("user_id", "document__path"):
        # Under a live entry point the document is the business of that entry
        if not any(path.startswith(root) for root in live.get(user_id, ())):
            deleted[user_id].add(path)

    paths = set().union(*live.values(), *deleted.values()) if users else set()
    documents = {d.path: d for d in models.Document.objects.filter(path__in=paths)}
    tree = (
        list(
            models.Document.objects.filter(path__startswith=within.path).values_list(
                "id", "path"
            )
        )
        if within is not None
        else []
    )

    result = {}
    for user in users:
        entries = []
        for path in sorted(live.get(user.pk, ())):
            role = RoleChoices.max(
                *(r for p, r in roles[user.pk] if path.startswith(p))
            )
            entries.append((documents[path], role, False))
        for path in sorted(deleted.get(user.pk, ())):
            entries.append((documents[path], RoleChoices.OWNER, True))
        if within is not None:
            entries = [e for e in entries if e[0].path.startswith(within.path)]
        taken = {str(e[0].pk) for e in entries}
        others = [str(pk) for pk, _ in tree if str(pk) not in taken]
        result[user.pk] = (entries, others)
    return result


def drives_of(document_ids):
    """The shared drives that carry a grant on each document."""
    drives = defaultdict(list)
    for grant in Grant.objects.filter(
        document_id__in=document_ids, source_kind=SourceKind.SHARED_DRIVE
    ).order_by("created_at"):
        drives[grant.document_id].append(
            {"source_id": grant.source_id, "owner_instance": grant.owner_instance}
        )
    return drives
