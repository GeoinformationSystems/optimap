# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Views for the curated /collections/ pages.

The detail page mirrors :func:`works.views_regions.continent_feed_page` so the
two surfaces feel consistent (map + work cards). Inline admin/curator
controls render on the index and detail pages for staff users; they POST to
the small mutation endpoints below (publish/unpublish, add/remove a work).
"""

import logging

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Count, OuterRef, Q, Subquery
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.html import strip_tags
from django.views.decorators.http import require_POST

from .models import STATUS_CHOICES, Collection, HarvestingEvent, Source, Work

User = get_user_model()
from .seo import coins_title
from .utils.geojson import publications_to_geojson
from .utils.geometry import annotate_rounded_geometry

logger = logging.getLogger(__name__)


def _visible_collections(request):
    """Anonymous users see only published collections; staff see everything."""
    qs = Collection.objects.all().prefetch_related("curators").select_related()
    if not (request.user.is_authenticated and request.user.is_staff):
        qs = qs.filter(is_published=True)
    return qs


def _collection_for_request(request, collection_slug):
    """Resolve a collection by identifier, enforcing visibility."""
    try:
        collection = Collection.objects.get(identifier=collection_slug)
    except Collection.DoesNotExist:
        raise Http404(f"Collection not found: {collection_slug}")
    is_admin = request.user.is_authenticated and request.user.is_staff
    if not collection.is_published and not is_admin:
        raise Http404(f"Collection not found: {collection_slug}")
    return collection


def collections_index(request):
    """List all collections. Staff see unpublished too, with inline controls."""
    is_admin = request.user.is_authenticated and request.user.is_staff
    collections = list(_visible_collections(request))
    if request.user.is_authenticated:
        curated_ids = set(request.user.curated_collections.values_list("id", flat=True))
    else:
        curated_ids = set()
    status_label = dict(STATUS_CHOICES)
    breakdown_order = ["p", "h", "c", "d", "t", "w"]
    for c in collections:
        c.show_breakdown = is_admin or c.id in curated_ids
        counts = {row["status"]: row["n"] for row in c.works.values("status").annotate(n=Count("id"))}
        c.published_count = counts.get("p", 0)
        if c.show_breakdown:
            c.status_breakdown = [
                {"status": s, "label": status_label[s], "count": counts[s]}
                for s in breakdown_order
                if counts.get(s, 0) > 0
            ]
        else:
            c.status_breakdown = []
    context = {
        "collections": collections,
        "is_admin": is_admin,
    }
    response = render(request, "collections.html", context)
    if is_admin:
        # Site-wide UpdateCacheMiddleware would otherwise cache this response
        # (keyed by session cookie) for CACHE_MIDDLEWARE_SECONDS, so the
        # admin's view of the page would not reflect publish/unpublish actions
        # until the entry expired.
        add_never_cache_headers(response)
    return response


def collection_page(request, collection_slug):
    """Detail page for one collection — map + work list + (for curators/admins) inline controls."""
    collection = _collection_for_request(request, collection_slug)
    is_admin = request.user.is_authenticated and request.user.is_staff
    is_curator = request.user.is_authenticated and collection.curators.filter(pk=request.user.pk).exists()

    # Visibility rule: anonymous / non-curators see only published works;
    # admins and curators of the collection see every work in the collection
    # so they can identify which ones still need review or publishing.
    can_curate = is_admin or is_curator
    works_qs = Work.objects.filter(collections=collection).select_related("source")
    if not can_curate:
        works_qs = works_qs.filter(status="p")
    works_qs = annotate_rounded_geometry(works_qs.order_by("-creationDate", "-id"))
    # Unfiltered total of works visible to this user — stays stable even when the
    # curation filter below narrows the displayed list.
    work_count_total = works_qs.count()

    # Counts for the curation publish helpers (shown to curators and admins):
    # only Harvested ('h') and Contributed ('c') — Draft / Testing / Withdrawn
    # are deliberate states and never auto-published.
    publishable_count = 0
    publishable_geo_count = 0
    publishable_geo_ids = []
    if can_curate:
        publishable_count = Work.objects.filter(collections=collection, status__in=["h", "c"]).count()
        candidate_qs = Work.objects.filter(collections=collection, status__in=["h", "c"]).only(
            "id", "geometry", "timeperiod_startdate", "timeperiod_enddate"
        )
        # Same extent predicate the "Publish N with extent" button uses, so the
        # filter below matches that button's target set 1:1.
        publishable_geo_ids = [
            w.pk
            for w in candidate_qs
            if (w.geometry and not w.geometry.empty)
            or any(d is not None for d in (w.timeperiod_startdate or []))
            or any(d is not None for d in (w.timeperiod_enddate or []))
        ]
        publishable_geo_count = len(publishable_geo_ids)

    # Optional curation filter: narrow the list to exactly the works the
    # "Publish N with extent" button acts on (Harvested/Contributed with extent).
    active_filter = request.GET.get("filter")
    if can_curate and active_filter == "publishable-extent":
        works_qs = works_qs.filter(pk__in=publishable_geo_ids)

    page_size = request.GET.get("size", settings.PAGE_MAX_ITEMS)
    try:
        page_size = int(page_size)
        page_size = max(settings.WORKS_PAGE_SIZE_MIN, min(page_size, settings.WORKS_PAGE_SIZE_MAX))
    except (ValueError, TypeError):
        page_size = settings.PAGE_MAX_ITEMS

    paginator = Paginator(works_qs, page_size)
    try:
        page_obj = paginator.page(request.GET.get("page", 1))
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    for w in page_obj.object_list:
        w.coins_ctx = coins_title(w)
        w.has_geo = bool(w.geometry and not w.geometry.empty)
        w.has_temporal = any(d is not None for d in (w.timeperiod_startdate or [])) or any(
            d is not None for d in (w.timeperiod_enddate or [])
        )

    # Per-source analytics — admin only.
    source_stats = []
    if is_admin:
        oai_types = {"oai-pmh", "ojs", "janeway"}
        # Include sources whose default collection is this one (FK) AND any
        # source that actually has works here — the two sets can diverge (e.g.
        # an intermediary platform harvested under a different Source record).
        # last_harvest_at as a correlated Subquery (not a plain annotate+Max)
        # so it doesn't join through HarvestingEvent into the same query as the
        # works__collections join above — that join already fans out one row
        # per work, and adding a second join for events would multiply rows
        # again before the GROUP BY collapses them back down.
        latest_event_at = (
            HarvestingEvent.objects.filter(source=OuterRef("pk")).order_by("-started_at").values("started_at")[:1]
        )
        sources = list(
            Source.objects.filter(Q(collection=collection) | Q(works__collections=collection))
            .distinct()
            .annotate(last_harvest_at=Subquery(latest_event_at))
        )
        # Per-source work counts in OPTIMAP — single query, avoid N+1.
        status_counts = (
            Work.objects.filter(collections=collection, source__in=sources)
            .values("source_id", "status")
            .annotate(n=Count("id"))
        )
        counts_by_source: dict[int, dict[str, int]] = {}
        for row in status_counts:
            counts_by_source.setdefault(row["source_id"], {})[row["status"]] = row["n"]
        for src in sources:
            stats_data = src.statistics or {}
            oa_count = stats_data.get("openalex_works_count")
            oai_count = stats_data.get("oai_works_count")
            crossref_count = stats_data.get("crossref_works_count")
            sc = counts_by_source.get(src.pk, {})
            source_stats.append(
                {
                    "name": src.name,
                    "source_type": src.source_type,
                    "openalex_id": src.openalex_id,
                    "last_harvest_at": src.last_harvest_at,
                    "has_openalex_count": oa_count is not None,
                    "openalex_works_count": oa_count,
                    "openalex_fetched_at": stats_data.get("openalex_fetched_at", "")[:10],
                    "is_oai": src.source_type in oai_types,
                    "has_oai_count": oai_count is not None,
                    "oai_works_count": oai_count,
                    "oai_fetched_at": stats_data.get("oai_fetched_at", "")[:10],
                    "is_crossref": src.source_type == "crossref-prefix",
                    "has_crossref_count": crossref_count is not None,
                    "crossref_works_count": crossref_count,
                    "crossref_fetched_at": stats_data.get("crossref_fetched_at", "")[:10],
                    "harvested_count": sc.get("h", 0),
                    "contributed_count": sc.get("c", 0),
                    "draft_count": sc.get("d", 0),
                    "published_count": sc.get("p", 0),
                }
            )

    context = {
        "collection": collection,
        "page_obj": page_obj,
        "page_size": page_size,
        "page_size_options": settings.WORKS_PAGE_SIZE_OPTIONS,
        "publications_geojson": publications_to_geojson(list(page_obj.object_list)),
        "collection_geojson_url": reverse("optimap:collection-geojson", args=[collection.identifier]),
        "is_admin": is_admin,
        "is_curator": is_curator,
        "can_curate": can_curate,
        "can_edit_description": is_admin or is_curator,
        "publishable_count": publishable_count,
        "publishable_geo_count": publishable_geo_count,
        "active_filter": active_filter,
        "work_count_total": work_count_total,
        "source_stats": source_stats,
        "canonical_url": request.build_absolute_uri(collection.get_absolute_url()),
        "curators": list(collection.curators.all()),
    }
    response = render(request, "collection_page.html", context)
    if can_curate:
        # Curators/admins see unpublished works and inline mutation controls
        # whose state must stay live; bypass the site-wide cache middleware.
        add_never_cache_headers(response)
    return response


def collection_geojson(request, collection_slug):
    """GeoJSON of all published works in a collection — used by the map 'show all' toggle."""
    collection = _collection_for_request(request, collection_slug)
    works_qs = annotate_rounded_geometry(
        Work.objects.filter(collections=collection, status="p").select_related("source")
    )
    return HttpResponse(publications_to_geojson(list(works_qs)), content_type="application/geo+json")


def collection_short_redirect(request, short_slug):
    """Vanity-URL handler: ``/<short_slug>/`` → 301 to canonical ``/collections/<identifier>/``.

    Returns 404 when no Collection has this short_slug, so the URL only resolves
    for collections the admin has explicitly opted in.
    """
    try:
        collection = Collection.objects.get(short_slug=short_slug, is_published=True)
    except Collection.DoesNotExist:
        raise Http404(f"No collection with short slug {short_slug!r}")
    return HttpResponseRedirect(collection.get_absolute_url())


def collection_by_id_redirect(request, collection_id):
    """``/collections/<int:id>/`` → 301 to canonical ``/collections/<identifier>/``.

    Lets internal links / admin tools / external citations refer to a collection
    by its database ID without breaking when the identifier changes — but the
    canonical URL the user sees is always the slug. Same visibility rules as
    the slug page: unpublished collections 404 for anonymous users.
    """
    collection = get_object_or_404(Collection, pk=collection_id)
    is_admin = request.user.is_authenticated and request.user.is_staff
    if not collection.is_published and not is_admin:
        raise Http404(f"Collection not found: {collection_id}")
    return HttpResponseRedirect(collection.get_absolute_url())


# --- Mutation endpoints ----------------------------------------------------


@staff_member_required
@require_POST
def publish_collection(request, collection_id):
    collection = get_object_or_404(Collection, pk=collection_id)
    collection.is_published = True
    collection.save(update_fields=["is_published", "updated_at"])
    return JsonResponse({"success": True, "is_published": True})


@staff_member_required
@require_POST
def unpublish_collection(request, collection_id):
    collection = get_object_or_404(Collection, pk=collection_id)
    collection.is_published = False
    collection.save(update_fields=["is_published", "updated_at"])
    return JsonResponse({"success": True, "is_published": False})


def _user_can_curate(user, collection):
    """A user is a curator of X iff they're listed in collection.curators (or are staff)."""
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    return collection.curators.filter(pk=user.pk).exists()


@login_required
@require_POST
def publish_collection_works(request, collection_id):
    """Curators and admins: bulk-set Harvested/Contributed works to Published.

    POST param ``extent_only=1``: restrict to works that have at least one real
    geometry point or a non-null temporal value (skips empty GeometryCollections
    and ``[None]`` date arrays). Omit or set to any other value to publish all.

    Targets ``status__in=['h', 'c']`` only — Draft / Testing / Withdrawn are
    admin-managed states and are deliberately left untouched.
    """
    collection = get_object_or_404(Collection, pk=collection_id)
    if not _user_can_curate(request.user, collection):
        return JsonResponse({"error": "Not a curator of this collection."}, status=403)
    if request.POST.get("extent_only") == "1":
        qs = Work.objects.filter(collections=collection, status__in=["h", "c"]).only(
            "geometry", "timeperiod_startdate", "timeperiod_enddate"
        )
        qualifying_ids = [
            w.pk
            for w in qs
            if (w.geometry and not w.geometry.empty)
            or any(d is not None for d in (w.timeperiod_startdate or []))
            or any(d is not None for d in (w.timeperiod_enddate or []))
        ]
        count = Work.objects.filter(pk__in=qualifying_ids).update(status="p")
    else:
        count = Work.objects.filter(collections=collection, status__in=["h", "c"]).update(status="p")
    return JsonResponse({"success": True, "published_count": count})


@login_required
@require_POST
def update_collection_description(request, collection_id):
    """Curators (and staff) update a collection's description.

    Plain text only — incoming HTML is stripped server-side via
    ``strip_tags``, then re-saved. The Django template ``{{ description }}``
    auto-escapes on render, but stripping at write time also keeps the
    persisted value clean (e.g. for API/feed consumers).
    """
    collection = get_object_or_404(Collection, pk=collection_id)
    if not _user_can_curate(request.user, collection):
        return JsonResponse({"error": "Not a curator of this collection."}, status=403)
    raw = request.POST.get("description", "")
    cleaned = strip_tags(raw).strip()
    collection.description = cleaned
    collection.save(update_fields=["description", "updated_at"])
    return JsonResponse({"success": True, "description": cleaned})


@login_required
@require_POST
def update_collection_logo(request, collection_id):
    """Curators (and staff) set or clear a collection's logo URL."""
    collection = get_object_or_404(Collection, pk=collection_id)
    if not _user_can_curate(request.user, collection):
        return JsonResponse({"error": "Not a curator of this collection."}, status=403)
    logo_url = request.POST.get("logo_url", "").strip()
    if logo_url:
        from django.core.exceptions import ValidationError
        from django.core.validators import URLValidator

        try:
            URLValidator()(logo_url)
        except ValidationError:
            return JsonResponse({"error": "Invalid URL."}, status=400)
    collection.logo_url = logo_url or None
    collection.save(update_fields=["logo_url", "updated_at"])
    return JsonResponse({"success": True, "logo_url": collection.logo_url or ""})


@login_required
@require_POST
def add_work_to_collection(request, work_id, collection_id):
    """Curator adds a work to their collection. Idempotent — adding a work
    that's already in the collection is a no-op. A work can be in multiple
    collections, so this never displaces an existing membership."""
    work = get_object_or_404(Work, pk=work_id)
    collection = get_object_or_404(Collection, pk=collection_id)
    if not _user_can_curate(request.user, collection):
        return JsonResponse({"error": "Not a curator of this collection."}, status=403)
    work.collections.add(collection)
    return JsonResponse(
        {
            "success": True,
            "work_id": work.id,
            "collection_id": collection.id,
            "collection_name": collection.name,
        }
    )


@login_required
@require_POST
def remove_work_from_collection(request, work_id, collection_id):
    work = get_object_or_404(Work, pk=work_id)
    collection = get_object_or_404(Collection, pk=collection_id)
    if not _user_can_curate(request.user, collection):
        return JsonResponse({"error": "Not a curator of this collection."}, status=403)
    if not work.collections.filter(pk=collection.pk).exists():
        return JsonResponse({"error": "Work is not in this collection."}, status=400)
    work.collections.remove(collection)
    return JsonResponse({"success": True, "work_id": work.id})


@login_required
@require_POST
def add_curator(request, collection_id):
    """Admin or existing curator adds another curator by email address."""
    from .notifications import notify_curator_change

    collection = get_object_or_404(Collection, pk=collection_id)
    if not _user_can_curate(request.user, collection):
        return JsonResponse({"error": "Not a curator of this collection."}, status=403)

    email = request.POST.get("email", "").strip()
    if not email:
        return JsonResponse({"error": "Email address is required."}, status=400)

    try:
        new_curator = User.objects.get(email=email)
    except User.DoesNotExist:
        return JsonResponse({"error": f"No user found with email: {email}"}, status=404)

    if collection.curators.filter(pk=new_curator.pk).exists():
        return JsonResponse(
            {"success": True, "already_curator": True, "user_id": new_curator.pk, "email": new_curator.email}
        )

    collection.curators.add(new_curator)
    notify_curator_change(collection, new_curator, "added", actor=request.user)
    return JsonResponse(
        {
            "success": True,
            "user_id": new_curator.pk,
            "email": new_curator.email,
            "display_name": new_curator.get_full_name() or new_curator.email,
        }
    )


@login_required
@require_POST
def remove_curator(request, collection_id, user_id):
    """Admin or existing curator removes a curator by user ID."""
    from .notifications import notify_curator_change

    collection = get_object_or_404(Collection, pk=collection_id)
    if not _user_can_curate(request.user, collection):
        return JsonResponse({"error": "Not a curator of this collection."}, status=403)

    curator_to_remove = get_object_or_404(User, pk=user_id)
    if not collection.curators.filter(pk=curator_to_remove.pk).exists():
        return JsonResponse({"error": "User is not a curator of this collection."}, status=400)

    collection.curators.remove(curator_to_remove)
    notify_curator_change(collection, curator_to_remove, "removed", actor=request.user)
    return JsonResponse({"success": True, "user_id": curator_to_remove.pk})
