# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Work-specific views.

This module handles:
- Work landing pages
- Work lists
- Work contribution pages
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

from django.conf import settings
from django.contrib import messages
from django.core.cache import caches
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.cache import add_never_cache_headers, patch_response_headers
from django.views.decorators.http import require_GET

from works.models import Collection, Work
from works.seo import (
    build_schema_org_for_work,
    build_work_meta,
    citation_meta_tags,
    coins_title,
    dc_coverage_tags,
    external_identifier_links,
    geo_meta_tags,
)
from works.serializers import get_available_gazetteers as _ner_available_gazetteers
from works.services.preview_image import (
    cache_path_for as _preview_cache_path,
)
from works.services.preview_image import (
    render_work_preview,
)
from works.utils.identifiers import resolve_work_for_landing, resolve_work_identifier
from works.utils.statistics import get_cached_statistics


def contribute(request):
    """Page showing harvested works that need spatial or temporal extent.

    Optional ``?collection=<id|identifier|short_slug>`` narrows the listing
    to a single Collection (anonymous / non-staff users only see published
    collections; unknown values surface a warning).
    """
    page_size = request.GET.get("size", settings.WORKS_PAGE_SIZE_DEFAULT)
    try:
        page_size = int(page_size)
        page_size = max(settings.WORKS_PAGE_SIZE_MIN, min(page_size, settings.WORKS_PAGE_SIZE_MAX))
    except (ValueError, TypeError):
        page_size = settings.WORKS_PAGE_SIZE_DEFAULT

    page_number = request.GET.get("page", 1)

    publications_query = (
        Work.objects.filter(
            status="h",
        )
        .filter(
            Q(geometry__isnull=True)
            | Q(geometry__isempty=True)
            | Q(timeperiod_startdate__isnull=True)
            | Q(timeperiod_enddate__isnull=True)
        )
        .order_by("-creationDate")
    )

    filter_collection = None
    filter_raw = request.GET.get("collection", "").strip()
    filter_invalid = False
    if filter_raw:
        is_admin = request.user.is_authenticated and request.user.is_staff
        candidates = Collection.objects.all() if is_admin else Collection.objects.filter(is_published=True)
        match = None
        if filter_raw.isdigit():
            match = candidates.filter(pk=int(filter_raw)).first()
        if match is None:
            match = (
                candidates.filter(identifier=filter_raw).first() or candidates.filter(short_slug=filter_raw).first()
            )
        if match is not None:
            filter_collection = match
            publications_query = publications_query.filter(collections=match)
        else:
            filter_invalid = True

    paginator = Paginator(publications_query, page_size)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    works = list(page_obj)
    for w in works:
        w.has_geo = bool(w.geometry and not w.geometry.empty)
        w.has_temporal = any(d is not None for d in (w.timeperiod_startdate or [])) or any(
            d is not None for d in (w.timeperiod_enddate or [])
        )

    context = {
        "works": works,
        "page_obj": page_obj,
        "page_size": page_size,
        "page_size_options": settings.WORKS_PAGE_SIZE_OPTIONS,
        "total_count": paginator.count,
        "filter_collection": filter_collection,
        "filter_raw": filter_raw,
        "filter_invalid": filter_invalid,
    }
    return render(request, "contribute.html", context)


def contribute_next(request):
    """Redirect to a random work needing geolocation — entry point for the georeferencing game.

    Accepts the same ``?collection=<id|identifier|short_slug>`` filter as
    ``contribute()``. When a logged-in user visits, works they have already
    made any contribution to are excluded (they either flipped to status 'c'
    already, or were reset by an admin).

    After picking a work, redirects to its landing page with
    ``?game=1&done=<N>&collection=<identifier>`` so the landing page can
    show the game banner, auto-run NER, and chain to the next work on submit.
    """
    is_admin = request.user.is_authenticated and request.user.is_staff
    filter_raw = request.GET.get("collection", "").strip()
    filter_collection = None
    if filter_raw:
        candidates = Collection.objects.all() if is_admin else Collection.objects.filter(is_published=True)
        if filter_raw.isdigit():
            filter_collection = candidates.filter(pk=int(filter_raw)).first()
        if filter_collection is None:
            filter_collection = (
                candidates.filter(identifier=filter_raw).first() or candidates.filter(short_slug=filter_raw).first()
            )

    qs = Work.objects.filter(status="h").filter(
        Q(geometry__isnull=True)
        | Q(geometry__isempty=True)
        | Q(timeperiod_startdate__isnull=True)
        | Q(timeperiod_enddate__isnull=True)
    )
    if filter_collection:
        qs = qs.filter(collections=filter_collection)
    if request.user.is_authenticated:
        qs = qs.exclude(contributions__user=request.user)

    work = qs.order_by("?").first()

    if work is None:
        messages.success(
            request,
            "Great job — all works in the queue are georeferenced! "
            "Check back later as new works are harvested regularly.",
        )
        dest = reverse("optimap:contribute")
        if filter_collection:
            dest += f"?collection={filter_collection.identifier}"
        return redirect(dest)

    done = request.GET.get("done", "0")
    params = ["game=1", f"done={done}"]
    if filter_collection:
        params.append(f"collection={filter_collection.identifier}")
    elif filter_raw:
        params.append(f"collection={filter_raw}")
    dest = reverse("optimap:work-landing", args=[work.get_identifier()]) + "?" + "&".join(params)
    return redirect(dest)


def _format_timeperiod(work):
    """
    Format all index-aligned (start, end) pairs from the ArrayField columns.
    Returns a "; "-separated string, or None when both arrays are empty.
    """
    s_list = work.timeperiod_startdate or []
    e_list = work.timeperiod_enddate or []
    n = max(len(s_list), len(e_list), 0)
    labels = []
    for i in range(n):
        s = (s_list[i] if i < len(s_list) else None) or None
        e = (e_list[i] if i < len(e_list) else None) or None
        if s and e:
            labels.append(f"{s} – {e}")
        elif s:
            labels.append(f"from {s}")
        elif e:
            labels.append(f"until {e}")
    return "; ".join(labels) or None


def _authors_for_display(work):
    """Return structured author list for the author_list.html partial.

    Delegates to ``works.services.authors.authors_for_display`` but catches all
    errors so a missing prefetch or unexpected model state never breaks the
    work landing page.
    """
    try:
        from works.services.authors import authors_for_display

        return authors_for_display(work)
    except Exception:
        return []


def _normalize_authors(work):
    """
    Try a few common attribute names. Accepts string (split on , or ;) or list/tuple.
    Returns list[str] or None.
    """
    candidates = (
        getattr(work, "authors", None),  # Primary: authors field
        getattr(work, "author", None),
        getattr(work, "creators", None),
        getattr(work, "creator", None),
    )
    raw = next((c for c in candidates if c), None)
    if not raw:
        return None
    if isinstance(raw, str):
        items = [x.strip() for x in re.split(r"[;,]", raw) if x.strip()]
        return items or None
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw if str(x).strip()]
        return items or None
    return None


def works_list(request):
    """
    Public page that lists all works with pagination:
    - DOI present  -> /work/<doi> (site-local landing page)
    - no DOI       -> fall back to Work.url (external/original)

    Only published works (status='p') are shown to non-admin users.
    Admin users see all works with status labels.

    Supports pagination with user-selectable page size.
    """
    is_admin = request.user.is_authenticated and request.user.is_staff

    # Get page size from request or use default
    page_size = request.GET.get("size", settings.WORKS_PAGE_SIZE_DEFAULT)
    try:
        page_size = int(page_size)
        # Clamp page size within allowed limits
        page_size = max(settings.WORKS_PAGE_SIZE_MIN, min(page_size, settings.WORKS_PAGE_SIZE_MAX))
    except (ValueError, TypeError):
        page_size = settings.WORKS_PAGE_SIZE_DEFAULT

    # Get page number from request
    page_number = request.GET.get("page", 1)

    # Base queryset
    if is_admin:
        pubs = Work.objects.all().select_related("source")
    else:
        pubs = Work.objects.filter(status="p").select_related("source")

    pubs = pubs.order_by("-creationDate", "-id")

    # Create paginator
    paginator = Paginator(pubs, page_size)

    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Build work data for current page
    is_authenticated = request.user.is_authenticated
    works = []
    for work in page_obj:
        work_data = {
            "title": work.title,
            "doi": work.doi,
            "authors": work.authors or [],
            "source": work.source.name if work.source else None,
            "href": reverse("optimap:work-landing", args=[work.get_identifier()]),
        }

        # Add status info for admin users
        if is_admin:
            work_data["status"] = work.get_status_display()
            work_data["status_code"] = work.status

        if is_authenticated:
            work_data["has_geo"] = bool(work.geometry and not work.geometry.empty)
            work_data["has_temporal"] = any(d is not None for d in (work.timeperiod_startdate or [])) or any(
                d is not None for d in (work.timeperiod_enddate or [])
            )

        works.append(work_data)

    # Get cached statistics
    stats = get_cached_statistics()

    # Build API URL for current page/size
    # DRF uses limit/offset pagination, so calculate offset from page number
    offset = (page_obj.number - 1) * page_size
    api_url = request.build_absolute_uri("/api/v1/works/" + f"?limit={page_size}&offset={offset}")

    context = {
        "works": works,
        "page_obj": page_obj,
        "page_size": page_size,
        "page_size_options": settings.WORKS_PAGE_SIZE_OPTIONS,
        "is_admin": is_admin,
        "is_authenticated": is_authenticated,
        "statistics": stats,
        "api_url": api_url,
    }

    return render(request, "works.html", context)


_WORK_LANDING_CACHE_TIMEOUT = 24 * 3600


def _work_landing_cache_key(work, request) -> str:
    """Cache key for the anonymous landing-page context.

    Includes:
    - ``request.get_host()`` so dev (127.0.0.1) and prod entries don't pollute
      each other — the cached payload contains absolute URLs built from the
      request host.
    - ``work.lastUpdate`` (auto-bumped to ``now()`` on every ``Work.save()``)
      so any edit immediately misses the old entry. No explicit invalidation
      signal needed; superseded entries age out via TTL.
    """
    return f"work_landing:ctx:{request.get_host()}:{work.id}:{work.lastUpdate.timestamp() if work.lastUpdate else 0}"


def _build_work_landing_cacheable(request, work, identifier_type):
    """Build the picklable, work-derived bits of the landing-page context.

    Excludes the ``Meta`` object (holds an unpicklable request reference)
    and the ``Work`` instance itself (re-fetched per request, cheap). The
    expensive ``schema.org`` JSON-LD dict is cached here; the view
    rebuilds the lightweight ``Meta`` object on each request and injects
    the cached schema via ``build_work_meta(request, work, kwargs_schema=...)``.
    """
    feature_json = None
    if work.geometry and not work.geometry.empty:
        feature = {
            "type": "Feature",
            "geometry": json.loads(work.geometry.geojson),
            "properties": {"title": work.title, "doi": work.doi or None},
        }
        feature_json = json.dumps(feature)

    bok_codes = list(work.bok_concepts or [])
    if bok_codes:
        try:
            from works.bok import client as bok_client

            bok_resolved = bok_client.resolve(bok_codes)
        except Exception:
            bok_resolved = [
                {"code": c, "name": c, "uri": "", "parent_code": "", "breadcrumb": [], "orphan": True}
                for c in bok_codes
            ]
    else:
        bok_resolved = []

    # Build index-aligned list of existing (start, end) pairs for the UI.
    s_list = work.timeperiod_startdate or []
    e_list = work.timeperiod_enddate or []
    existing_periods = [
        {
            "start": (s_list[i] if i < len(s_list) else None) or "",
            "end": (e_list[i] if i < len(e_list) else None) or "",
        }
        for i in range(max(len(s_list), len(e_list)))
    ]

    return {
        "feature_json": feature_json,
        "timeperiod_label": _format_timeperiod(work),
        "existing_periods": existing_periods,
        "existing_periods_json": json.dumps(existing_periods),
        "authors_list": _normalize_authors(work),
        "authors_display": _authors_for_display(work),
        "has_geometry": bool(work.geometry and not work.geometry.empty),
        "has_temporal": (
            any(d is not None for d in (work.timeperiod_startdate or []))
            or any(d is not None for d in (work.timeperiod_enddate or []))
        ),
        "use_id_urls": not work.doi,
        "identifier_type": identifier_type,
        "schema_org": build_schema_org_for_work(work, request),
        "citation_tags": citation_meta_tags(work, request),
        "dc_coverage_tags": dc_coverage_tags(work),
        "geo_tags": geo_meta_tags(work),
        "alternate_links": external_identifier_links(work),
        "coins_ctx": coins_title(work),
        "canonical_url": request.build_absolute_uri(reverse("optimap:work-landing", args=[work.get_identifier()])),
        "bok_concepts_resolved": bok_resolved,
        "bok_initial_codes_json": json.dumps(bok_codes),
    }


def work_landing(request, identifier):
    """
    Landing page for a work accessed by various identifier types.

    Tries to resolve the identifier in this order:
    1. DOI (if identifier contains '/' or starts with '10.')
    2. Internal database ID (if identifier is numeric)
    3. Handle (placeholder for future implementation)

    Embeds a small Leaflet map when geometry is available.

    Only published works (status='p') are accessible to non-admin users.
    Admin users can view all works with a status label.

    For anonymous requests the work-derived part of the context is cached
    in the in-memory backend (key ``work_landing:ctx:<host>:<work.id>``)
    and invalidated by ``works.signals.invalidate_work_caches`` on every
    ``Work`` save. Authenticated and staff requests always render live to
    keep status badges, publish buttons, and provenance current.
    """

    is_admin = request.user.is_authenticated and request.user.is_staff

    # Game-mode params — read before any expensive DB work so they're
    # available throughout the rest of the view.
    game_mode = request.GET.get("game") == "1"
    game_done = max(0, int(request.GET.get("done", "0") or 0))
    game_coll_raw = request.GET.get("collection", "").strip()

    # Resolve identifier to work object. A merged-away duplicate (or an alternate
    # identifier of one) 302-redirects to the canonical work so every identifier
    # of a work lands on one page.
    work, identifier_type, should_redirect = resolve_work_for_landing(identifier)
    if should_redirect:
        target = reverse("optimap:work-landing", args=[work.get_identifier()])
        if request.META.get("QUERY_STRING"):
            target = f"{target}?{request.META['QUERY_STRING']}"
        return redirect(target)  # 302: merges are reversible (unmerge)

    # Visibility: 'p' (Published) is fully public. 'h' (Harvested) and 'c'
    # (Contributed) are also visible to non-admins so the /contribute/ flow
    # can hand the user from the listing to the work landing page (where the
    # contribution form lives) — and so a successful contribution that flips
    # a work from 'h' to 'c' does not 404 the user on the post-reload. Drafts
    # ('d'), Testing ('t'), and Withdrawn ('w') remain admin-only.
    if not is_admin and work.status not in ("p", "h", "c"):
        raise Http404("Work not found.")

    is_anonymous = not request.user.is_authenticated
    cache_backend = caches["memory"]
    cache_key = _work_landing_cache_key(work, request) if is_anonymous else None

    cacheable = None
    if cache_key:
        cacheable = cache_backend.get(cache_key)
    if cacheable is None:
        cacheable = _build_work_landing_cacheable(request, work, identifier_type)
        if cache_key:
            cache_backend.set(cache_key, cacheable, timeout=_WORK_LANDING_CACHE_TIMEOUT)

    # Rebuild Meta per request — cheap; the heavy schema dict comes from
    # the cache.
    meta = build_work_meta(request, work, kwargs_schema=cacheable["schema_org"])

    # User-dependent overlay — never cached.
    # Contribution editor is open on harvested AND contributed works so
    # different users can fill different gaps (spatial, temporal, topics).
    # Pre-existing extents do NOT close the form: replacing user A's
    # geometry as user B is allowed; the provenance log records who did
    # what, and the Recognition Board dedupes per (user, work, kind).
    can_contribute = request.user.is_authenticated and (work.status in ("h", "c") or (is_admin and work.status == "d"))
    # Anonymous visitors who land on a contributable work via the /contribute/
    # listing get a "log in to contribute" call-to-action instead of a silent
    # "no form here" page.
    prompt_login_to_contribute = not request.user.is_authenticated and work.status in ("h", "c")
    is_curator = (
        request.user.is_authenticated and not is_admin and work.collections.filter(curators=request.user).exists()
    )
    can_publish = (is_admin or is_curator) and (
        work.status in ("c", "d") or (work.status == "h" and (cacheable["has_geometry"] or cacheable["has_temporal"]))
    )
    can_unpublish = is_admin and work.status == "p"
    # Admins can re-harvest any DOI-bearing work to refresh its metadata from
    # Crossref + enrichment (OpenAlex/OpenAIRE), synchronously.
    can_reharvest = is_admin and bool(work.doi)

    # BoK tagging is open to any logged-in user while a work is still in the
    # contribution pipeline (h or c). Admins keep full control via the admin.
    # When OPTIMAP_BOK_ENABLED_COLLECTIONS is set, only works belonging to one
    # of the configured collections expose the editor.
    from works.bok import eligibility as bok_eligibility

    bok_eligible = bok_eligibility.is_work_eligible(work)
    can_tag_bok = (
        request.user.is_authenticated
        and (work.status in ("h", "c") or (is_admin and work.status == "d"))
        and bok_eligible
    )
    prompt_login_to_tag_bok = not request.user.is_authenticated and work.status in ("h", "c") and bok_eligible

    # Single source of truth for the "missing information" alert on the
    # landing page — items the *current* viewer could fix if they were to
    # use the contribution form (or log in first). Anonymous viewers see
    # the same item list with a "log in to contribute" CTA. Each item
    # carries the in-page anchor for a "jump to that section" link.
    has_bok = bool(cacheable.get("bok_concepts_resolved"))
    _GEOM = {"label": "geographic location", "anchor": "contribute-spatial"}
    _TIME = {"label": "temporal extent (time period)", "anchor": "contribute-temporal"}
    _BOK = {"label": "topics (EO4GEO BoK)", "anchor": "bok-edit-card"}

    missing_for_logged_in = []
    if can_contribute and not cacheable["has_geometry"]:
        missing_for_logged_in.append(_GEOM)
    if can_contribute and not cacheable["has_temporal"]:
        missing_for_logged_in.append(_TIME)
    if can_tag_bok and not has_bok:
        missing_for_logged_in.append(_BOK)

    missing_for_anonymous = []
    if prompt_login_to_contribute and not cacheable["has_geometry"]:
        missing_for_anonymous.append(_GEOM)
    if prompt_login_to_contribute and not cacheable["has_temporal"]:
        missing_for_anonymous.append(_TIME)
    if prompt_login_to_tag_bok and not has_bok:
        missing_for_anonymous.append(_BOK)

    latest_wikidata_export = (
        work.wikidata_exports.filter(action__in=["created", "updated"]).order_by("-export_date").first()
    )
    all_wikidata_exports = work.wikidata_exports.all() if is_admin else []

    # Collections this work belongs to — hidden unpublished collections from
    # anonymous users so visibility rules match /collections/ and the
    # collection detail page. Computed per request (not cached) because
    # collection.is_published can flip without bumping Work.lastUpdate.
    visible_collections_qs = work.collections.all().order_by("name")
    if not is_admin:
        visible_collections_qs = visible_collections_qs.filter(is_published=True)
    visible_collections = list(visible_collections_qs)

    # Build game-mode URLs. game_next_url (used after a successful contribution)
    # increments done; game_skip_url (used by the Skip button) preserves the count.
    base_game_params = []
    if game_coll_raw:
        base_game_params.append(f"collection={game_coll_raw}")
    contribute_next_base = reverse("optimap:contribute-next")
    game_next_params = [f"done={game_done + 1}"] + base_game_params
    game_skip_params = [f"done={game_done}"] + base_game_params
    game_next_url = contribute_next_base + "?" + "&".join(game_next_params)
    game_skip_url = contribute_next_base + "?" + "&".join(game_skip_params)

    context = {
        **{k: v for k, v in cacheable.items() if k != "schema_org"},
        "work": work,
        "meta": meta,
        "is_admin": is_admin,
        "status_display": work.get_status_display() if is_admin else None,
        "can_contribute": can_contribute,
        "prompt_login_to_contribute": prompt_login_to_contribute,
        "can_publish": can_publish,
        "can_unpublish": can_unpublish,
        "can_reharvest": can_reharvest,
        "can_tag_bok": can_tag_bok,
        "prompt_login_to_tag_bok": prompt_login_to_tag_bok,
        "missing_for_logged_in": missing_for_logged_in,
        "missing_for_anonymous": missing_for_anonymous,
        "geoextent_copy_ttl_seconds": getattr(settings, "GEOEXTENT_COPY_TTL_SECONDS", 300),
        "geometry_warn_size_kb": getattr(settings, "GEOMETRY_WARN_SIZE_KB", 50),
        "geometry_max_upload_kb": getattr(settings, "GEOMETRY_MAX_UPLOAD_KB", 2048),
        "ner_available_gazetteers": _ner_available_gazetteers(),
        "show_provenance": is_admin,
        "latest_wikidata_export": latest_wikidata_export,
        "all_wikidata_exports": all_wikidata_exports,
        "visible_collections": visible_collections,
        "game_mode": game_mode,
        "game_done": game_done,
        "game_next_url": game_next_url,
        "game_skip_url": game_skip_url,
    }
    response = render(request, "work_landing_page.html", context)
    # W3C SDW-BP 5: link to machine-readable GeoJSON representation.
    if work.geometry and not work.geometry.empty:
        api_url = request.build_absolute_uri(reverse("optimap:works:work-detail", args=[work.id]))
        response["Link"] = f'<{api_url}>; rel="alternate"; type="application/geo+json"'
    if is_anonymous:
        # Mirror the server-side TTL into ``Cache-Control: max-age=…`` and
        # ``Expires`` so browsers and intermediaries can co-cache. Saves
        # served from the cache key for ``Work.lastUpdate`` change as soon
        # as the work is edited, but downstream caches won't see that —
        # so they'll keep serving the stale entry until ``max-age`` expires.
        # 24h matches the server cache TTL.
        patch_response_headers(response, cache_timeout=_WORK_LANDING_CACHE_TIMEOUT)
    else:
        # Authenticated users see inline mutation controls (publish /
        # unpublish, contribute, add/remove from collection) whose state
        # must reflect the database after a POST. Without this, the
        # site-wide UpdateCacheMiddleware would cache the response keyed
        # by session cookie and the reloaded GET would short-circuit on
        # the stale entry until CACHE_MIDDLEWARE_SECONDS expired.
        add_never_cache_headers(response)
    return response


@require_GET
def work_preview_png(request, identifier):
    """og:image preview for work landing pages — issue #22.

    Returns a 1200×630 PNG of the work's spatial extent. Cached lazily on
    disk; invalidated by the post_save signal in ``works/signals.py``.
    Returns 404 when the work has no geometry — landings for those works
    don't emit an og:image so this endpoint should not be hit for them.
    """

    work, _ = resolve_work_identifier(identifier)
    # Same visibility rule as work_landing — preview is the og:image for the
    # landing page, so it has to be reachable wherever the landing page is.
    if not (request.user.is_authenticated and request.user.is_staff) and work.status not in ("p", "h", "c"):
        raise Http404("Work not found.")
    if not work.geometry or work.geometry.empty:
        raise Http404("Work has no geometry — no preview available.")

    cache_file = _preview_cache_path(work)
    if not cache_file.exists():
        try:
            data = render_work_preview(work)
        except Exception as err:
            logger.warning("preview render failed for work %s: %s", work.id, err)
            raise Http404("Preview unavailable.") from err
        cache_file.write_bytes(data)

    response = FileResponse(open(cache_file, "rb"), content_type="image/png")
    response["Cache-Control"] = "public, max-age=3600"
    return response


def statistics_page(request):
    """Standalone statistics page showing snapshot data with Chart.js plots."""
    import json as _json

    from works.models import GlobalRegion, SourceCoverageSnapshot, StatisticsSnapshot

    try:
        snapshot = StatisticsSnapshot.objects.latest()
    except StatisticsSnapshot.DoesNotExist:
        snapshot = None

    # Last 90 snapshots for time-series charts (one per day ≈ 3 months)
    history = list(
        StatisticsSnapshot.objects.order_by("computed_at")
        .values(
            "computed_at",
            "published_works",
            "total_works",
            "with_geometry",
            "with_temporal",
            "contributors",
            "contributed_dois",
        )
        .reverse()[:90]
    )
    history.reverse()  # chronological order for Chart.js

    # Latest coverage snapshot per source, only those with data
    sources_coverage = list(
        SourceCoverageSnapshot.objects.filter(openalex_total__gt=0)
        .select_related("source")
        .order_by("source_id", "-computed_at")
        .distinct("source_id")
    )

    # Per-source history for the dropdown chart (last 52 weekly snapshots each)
    source_history = {}
    for snap in sources_coverage:
        history_qs = list(
            SourceCoverageSnapshot.objects.filter(source=snap.source)
            .order_by("computed_at")
            .values("computed_at", "coverage_pct", "optimap_count", "openalex_total")
            .reverse()[:52]
        )
        history_qs.reverse()
        source_history[snap.source_id] = {
            "name": snap.source.name,
            "history": [
                {
                    "date": str(h["computed_at"].date()),
                    "coverage_pct": h["coverage_pct"],
                    "optimap_count": h["optimap_count"],
                    "openalex_total": h["openalex_total"],
                }
                for h in history_qs
            ],
        }

    # Annotate by_continent / by_ocean entries with the region landing-page URL
    # so the template can render names as links without template-dict lookups.
    region_urls = {r.name: r.get_absolute_url() for r in GlobalRegion.objects.all()}

    def _with_url(rows):
        return [{"name": r["name"], "count": r["count"], "url": region_urls.get(r["name"])} for r in rows]

    snapshot_by_continent = _with_url(snapshot.by_continent) if snapshot else []
    snapshot_by_ocean = _with_url(snapshot.by_ocean) if snapshot else []

    # by_country uses ISO codes as "name"; map to /at/<country>/ pages and show
    # the full country name where we know it.
    from works.models import Country, Source

    # only name+iso_code+slug are needed; defer the (large) geometry column.
    country_by_code = {c.iso_code: c for c in Country.objects.real().only("iso_code", "name", "slug")}

    def _with_country(rows):
        out = []
        for r in rows:
            country = country_by_code.get(r["name"])
            out.append(
                {
                    "name": country.name if country else r["name"],
                    "code": r["name"],
                    "count": r["count"],
                    "url": country.get_absolute_url() if country else None,
                }
            )
        return out

    # by_journal uses source names; link to /in/<slug>/ where the name matches a Source.
    source_urls = {
        s.name: s.get_absolute_url() for s in Source.objects.exclude(slug__isnull=True).only("name", "slug")
    }

    def _with_source(rows):
        return [{"name": r["name"], "count": r["count"], "url": source_urls.get(r["name"])} for r in rows]

    snapshot_by_country = _with_country(snapshot.by_country) if snapshot else []
    snapshot_by_journal = _with_source(snapshot.by_journal) if snapshot else []

    # by_topic links to the /on/<slug> topic facet page (only when the topic's
    # slug actually resolves there, mirroring the /browse directory); by_year
    # links to the /during/<year> data-year facet page.
    from django.urls import reverse
    from django.utils.text import slugify

    from works.views_indexed import topic_slug_map

    topic_slugs = set(topic_slug_map())

    def _with_topic(rows):
        out = []
        for r in rows:
            slug = slugify(r["name"])
            url = reverse("optimap:on-topic", args=[slug]) if slug in topic_slugs else None
            out.append({"name": r["name"], "count": r["count"], "url": url})
        return out

    def _with_year(rows):
        return [
            {"year": r["year"], "count": r["count"], "url": reverse("optimap:during-year", args=[r["year"]])}
            for r in rows
        ]

    snapshot_by_topic = _with_topic(snapshot.by_topic) if snapshot else []
    snapshot_by_year = _with_year(snapshot.by_year) if snapshot else []

    return render(
        request,
        "statistics.html",
        {
            "snapshot": snapshot,
            "snapshot_by_continent": snapshot_by_continent,
            "snapshot_by_ocean": snapshot_by_ocean,
            "snapshot_by_country": snapshot_by_country,
            "snapshot_by_journal": snapshot_by_journal,
            "snapshot_by_topic": snapshot_by_topic,
            "snapshot_by_year": snapshot_by_year,
            "history_json": _json.dumps(
                [
                    {
                        "date": str(h["computed_at"].date()),
                        "published_works": h["published_works"],
                        "total_works": h["total_works"],
                        "with_geometry": h["with_geometry"],
                        "with_temporal": h["with_temporal"],
                        "contributors": h["contributors"],
                        "contributed_dois": h["contributed_dois"],
                    }
                    for h in history
                ]
            ),
            "sources_coverage": sources_coverage,
            "source_history_json": _json.dumps(source_history),
        },
    )
