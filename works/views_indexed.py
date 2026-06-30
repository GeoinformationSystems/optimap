# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Faceted permalink pages (issue #29): /at/, /during/, /on/, and /browse/.

Short, SEO-friendly, linkable URLs that render a filtered list of *published*
works:

- ``/at/<place>``   — works in a continent/ocean (GlobalRegion) or a country
- ``/during/<year>``— works whose **temporal coverage** (data years) covers the
  year — NOT the publication date (see :func:`works_covering_year`)
- ``/on/<topic>``   — works tagged with an OpenAlex topic (``Work.topics``)
- ``/browse/``      — a directory of all facets with counts

The source facet ``/in/<slug>`` lives in :mod:`works.views_sources` because it
also carries coverage statistics and feeds (#253). All pages are list-only and
get SEO metadata via :func:`works.seo.build_facet_page_meta`.
"""

import datetime
import json
import logging
import re
from collections import Counter

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.cache import cache_page, never_cache
from django.views.decorators.http import require_POST
from django_q.humanhash import humanize as humanize_task_id
from django_q.tasks import async_task

from .feeds import get_region_from_slug, normalize_region_slug
from .models import SENTINEL_COUNTRY_ISO, Author, Country, GlobalRegion, Source, StatisticsSnapshot, Work
from .seo import build_facet_page_meta, coins_title
from .utils.geojson import build_works_map_context
from .utils.geometry import annotate_rounded_geometry
from .utils.pagination import paginate_works
from .utils.provenance import append_event, set_block
from .views_regions import _get_regional_publications

logger = logging.getLogger(__name__)

YEAR_MIN = 1900
_YEAR_RE = re.compile(r"(\d{4})")
_YEAR_CACHE_TIMEOUT = 60 * 60  # 1 hour — data years change rarely


# --- temporal-coverage (data year) helpers ---------------------------------


def _year_of(datestr):
    """Extract a 4-digit year from a stored date string, or None."""
    if not datestr:
        return None
    m = _YEAR_RE.search(str(datestr))
    return int(m.group(1)) if m else None


def _work_year_ranges(work):
    """Yield (start_year, end_year) for each temporal interval of a work.

    ``timeperiod_startdate``/``timeperiod_enddate`` are parallel ArrayFields of
    date strings; either bound may be missing (open-ended interval). An interval
    with no usable bound at all is skipped.
    """
    starts = work.timeperiod_startdate or []
    ends = work.timeperiod_enddate or []
    for i in range(max(len(starts), len(ends))):
        sy = _year_of(starts[i]) if i < len(starts) else None
        ey = _year_of(ends[i]) if i < len(ends) else None
        if sy is None and ey is None:
            continue
        yield (sy if sy is not None else ey, ey if ey is not None else sy)


def _published_works_with_temporal():
    """Published works that have at least one temporal bound (candidate set)."""
    return Work.objects.filter(status="p").exclude(timeperiod_startdate__isnull=True).exclude(timeperiod_startdate=[])


def _covered_years(work):
    """Set of in-range data years a work's temporal coverage spans.

    Each interval is clamped to ``[YEAR_MIN, current_year + 1]`` (rather than
    dropped when it partly falls outside), so this is the single definition of
    "which years a work covers" shared by both :func:`works_covering_year`
    (the /during/<year> page) and :func:`data_year_counts` (the /browse
    directory and ``YearSitemap``) — the two can no longer disagree.
    """
    upper = datetime.date.today().year + 1
    years = set()
    for start, end in _work_year_ranges(work):
        lo, hi = (start, end) if start <= end else (end, start)
        lo = max(lo, YEAR_MIN)
        hi = min(hi, upper)
        if lo <= hi:
            years.update(range(lo, hi + 1))
    return years


def works_covering_year(year):
    """PKs of published works whose temporal coverage covers ``year`` (cached)."""
    cache_key = f"facet:data_year_pks:{year}"
    pks = cache.get(cache_key)
    if pks is not None:
        return pks
    pks = [
        w.pk
        for w in _published_works_with_temporal().only("id", "timeperiod_startdate", "timeperiod_enddate")
        if year in _covered_years(w)
    ]
    cache.set(cache_key, pks, _YEAR_CACHE_TIMEOUT)
    return pks


def data_year_counts():
    """{year: count} of published works per data year covered (cached)."""
    cache_key = "facet:data_year_counts"
    counts = cache.get(cache_key)
    if counts is not None:
        return counts
    counter = Counter()
    for w in _published_works_with_temporal().only("id", "timeperiod_startdate", "timeperiod_enddate"):
        for y in _covered_years(w):
            counter[y] += 1
    counts = dict(counter)
    cache.set(cache_key, counts, _YEAR_CACHE_TIMEOUT)
    return counts


# --- topic helpers (single scan of Work.topics, cached) --------------------


def published_topic_counts():
    """{canonical_topic: count} across published works' OpenAlex topics (cached).

    The single source of truth for topics, shared by the /on/<topic> resolver,
    the /browse directory, and ``TopicSitemap`` so all three list/resolve the
    same set (a slug advertised in one can't 404 in another).
    """
    cache_key = "facet:topic_counts"
    counts = cache.get(cache_key)
    if counts is not None:
        return counts
    counter = Counter()
    for topics in (
        Work.objects.filter(status="p")
        .exclude(topics__isnull=True)
        .exclude(topics=[])
        .values_list("topics", flat=True)
    ):
        for topic in topics or []:
            counter[topic] += 1
    counts = dict(counter)
    cache.set(cache_key, counts, _YEAR_CACHE_TIMEOUT)
    return counts


def topic_slug_map():
    """{slug: canonical_topic} for all published topics (cached, slug wins last)."""
    cache_key = "facet:topic_slug_map"
    mapping = cache.get(cache_key)
    if mapping is not None:
        return mapping
    mapping = {}
    for topic in published_topic_counts():
        slug = slugify(topic)
        if slug:
            mapping[slug] = topic
    cache.set(cache_key, mapping, _YEAR_CACHE_TIMEOUT)
    return mapping


# --- shared rendering ------------------------------------------------------


def _paginate(request, works):
    return paginate_works(request, works, decorate=lambda w: setattr(w, "coins_ctx", coins_title(w)))


def _render_facet(
    request, *, page_url, heading, lead, title, description, works, extra=None, with_map=False, map_cache_key=None
):
    page_obj, page_size = _paginate(request, works)
    meta = build_facet_page_meta(request, title=title, description=description, page_url=page_url)
    context = {
        "facet_heading": heading,
        "facet_lead": lead,
        "page_obj": page_obj,
        "page_size": page_size,
        "page_size_options": settings.WORKS_PAGE_SIZE_OPTIONS,
        "work_count_total": page_obj.paginator.count,
        "meta": meta,
        "canonical_url": request.build_absolute_uri(page_url),
    }
    if with_map:
        context.update(
            build_works_map_context(
                page_obj.object_list,
                works,
                page_url,
                all_cache_key=map_cache_key,
                force_refresh=request.GET.get("now") is not None,
            )
        )
    if extra:
        context.update(extra)
    return render(request, "indexed_page.html", context)


# --- facet views -----------------------------------------------------------


def place_page(request, place_slug):
    """Works in a continent/ocean (GlobalRegion) or a country (by Work.countries).

    An ISO 3166-1 alpha-2 code (e.g. ``/at/DE``) 301-redirects to the canonical
    country-name slug (``/at/germany``).
    """
    # ISO alpha-2 shortcode → permanent redirect to the canonical name slug.
    if len(place_slug) == 2 and place_slug.isalpha():
        by_code = Country.objects.real().filter(iso_code=place_slug.upper()).first()
        if by_code is not None:
            return redirect(by_code.get_absolute_url(), permanent=True)

    # Country-first: when a name is both a country and a continent (e.g.
    # "Australia"), the country view (by Work.countries) wins so its work count
    # matches the count shown on /countries. Continents/oceans keep their own
    # spatial landing pages under /regions/ (linked from the place index).
    # Lookup is via the indexed Country.slug, not a full-table scan.
    normalized = normalize_region_slug(place_slug)
    country = Country.objects.real().filter(slug=normalized).first()
    is_country = country is not None
    extra = {"show_place_nav": True}
    if is_country:
        works = annotate_rounded_geometry(
            Work.objects.filter(status="p", countries=country)
            .select_related("source")
            .order_by("-creationDate", "-id")
        )
        kind = "Country"
        name = country.name
        # The country outline is loaded onto the facet map (from the shared,
        # browser-cached /api/v1/countries/ data) so the page always shows a
        # map even when no works carry geometry.
        extra["facet_country_iso"] = country.iso_code
    else:
        region = get_region_from_slug(place_slug)
        if region is None:
            raise Http404(f"Place not found: {place_slug}")
        works = _get_regional_publications(region)
        kind = region.get_region_type_display()
        name = region.name

    page_url = reverse("optimap:at-place", kwargs={"place_slug": place_slug})
    return _render_facet(
        request,
        page_url=page_url,
        heading=f"Works in {name}",
        lead=f"Published research works with metadata for {name} ({kind}).",
        title=f"{name} — works on OPTIMAP",
        description=f"Published research works with geographic metadata for {name}.",
        works=works,
        extra=extra,
        with_map=True,
        map_cache_key=f"facet_map_all:place:{normalized}",
    )


def year_page(request, year):
    """Works whose temporal coverage (data years) covers ``year``."""
    if year < YEAR_MIN or year > datetime.date.today().year + 1:
        raise Http404(f"Year out of range: {year}")
    works = Work.objects.filter(pk__in=works_covering_year(year)).order_by("-creationDate", "-id")
    page_url = reverse("optimap:during-year", kwargs={"year": year})
    return _render_facet(
        request,
        page_url=page_url,
        heading=f"Works covering {year}",
        lead=f"Published research works whose temporal coverage includes the year {year}.",
        title=f"{year} — works on OPTIMAP",
        description=f"Published research works whose data covers the year {year}.",
        works=works,
    )


def topic_page(request, topic_slug):
    """Works tagged with an OpenAlex topic (``Work.topics``)."""
    topic = topic_slug_map().get(topic_slug)
    if topic is None:
        raise Http404(f"Topic not found: {topic_slug}")
    works = Work.objects.filter(status="p", topics__contains=[topic]).order_by("-creationDate", "-id")
    page_url = reverse("optimap:on-topic", kwargs={"topic_slug": topic_slug})
    return _render_facet(
        request,
        page_url=page_url,
        heading=f"Works on {topic}",
        lead=f"Published research works tagged with the topic “{topic}”.",
        title=f"{topic} — works on OPTIMAP",
        description=f"Published research works on the topic {topic}.",
        works=works,
    )


def browse_page(request):
    """Directory of all facets (places, years, sources, topics) with counts."""
    try:
        snapshot = StatisticsSnapshot.objects.latest()
    except StatisticsSnapshot.DoesNotExist:
        snapshot = None

    # Places: continents + oceans (GlobalRegion) and countries with published works.
    region_counts = {}
    if snapshot:
        for row in (snapshot.by_continent or []) + (snapshot.by_ocean or []):
            region_counts[row["name"]] = row["count"]
    regions = [
        {
            "name": region.name,
            "slug": region.get_slug(),
            "type": region.get_region_type_display(),
            "count": region_counts.get(region.name, 0),
        }
        for region in GlobalRegion.objects.all()
    ]

    country_counts = {}
    if snapshot:
        country_counts = {row["name"]: row["count"] for row in (snapshot.by_country or [])}
    countries = [
        {"name": c.name, "slug": c.slug, "count": country_counts.get(c.iso_code, 0)}
        for c in Country.objects.real().only("name", "slug", "iso_code")
        if country_counts.get(c.iso_code, 0)
    ]
    countries.sort(key=lambda x: (-x["count"], x["name"]))

    # Years: from temporal coverage (data years), not publicationDate.
    counts = data_year_counts()
    years = [{"year": y, "count": counts[y]} for y in sorted(counts, reverse=True)]

    # Sources: those with a slug and at least one published work.
    sources = list(
        Source.objects.exclude(slug__isnull=True)
        .annotate(n=Count("works", filter=Q(works__status="p")))
        .filter(n__gt=0)
        .order_by("-n", "name")
        .values("name", "slug", "n")
    )

    # Topics: shared cached scan (same set as /on/<topic> and TopicSitemap).
    topics = [
        {"name": name, "slug": slugify(name), "count": count}
        for name, count in sorted(published_topic_counts().items(), key=lambda kv: (-kv[1], kv[0]))
        if slugify(name)
    ]

    page_url = reverse("optimap:browse")
    meta = build_facet_page_meta(
        request,
        title="Browse OPTIMAP — places, years, sources, topics",
        description="Browse research works on OPTIMAP by place, year, source, and topic.",
        page_url=page_url,
    )
    context = {
        "regions": regions,
        "countries": countries,
        "years": years,
        "sources": sources,
        "topics": topics,
        "meta": meta,
        "canonical_url": request.build_absolute_uri(page_url),
    }
    return render(request, "browse.html", context)


# --- place / country / source index pages ----------------------------------

# Display order for continents on the /countries/ and /at/ overviews; anything
# not listed (e.g. "Seven seas (open ocean)") is appended alphabetically.
CONTINENT_ORDER = ["Africa", "Asia", "Europe", "North America", "South America", "Oceania", "Antarctica"]


# ISO-like codes present in the Natural Earth data that are NOT valid ISO 3166-1
# regions, so no flag emoji exists for them.
_NO_FLAG_CODES = {"XK"}


def _flag_emoji(iso_code):
    """Unicode flag emoji for an ISO 3166-1 alpha-2 code (regional indicators).

    Returns "" for codes that have no flag emoji — currently only user-assigned
    codes like ``XK`` (Kosovo), which are not valid ISO regions.
    """
    code = (iso_code or "").upper()
    if len(code) != 2 or not code.isalpha() or code in _NO_FLAG_CODES:
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code)


def _published_country_counts():
    """{iso_code: published-work count} for the country overviews."""
    rows = (
        Work.objects.filter(status="p", countries__isnull=False)
        .exclude(countries__iso_code=SENTINEL_COUNTRY_ISO)
        .values("countries__iso_code")
        .annotate(n=Count("id", distinct=True))
    )
    return {r["countries__iso_code"]: r["n"] for r in rows}


def _countries_by_continent():
    """Group all countries by continent, with the continent's landing-page URL.

    Each group: {"continent", "landing_url" (continent region page or None),
    "countries": [{"name", "slug", "iso", "count"}]}. Ordered by CONTINENT_ORDER.
    """
    counts = _published_country_counts()
    landing = {r.name: r.get_absolute_url() for r in GlobalRegion.objects.filter(region_type=GlobalRegion.CONTINENT)}
    groups = {}
    for c in Country.objects.real().only("name", "slug", "iso_code", "continent").order_by("name"):
        groups.setdefault(c.continent or "Other", []).append(
            {
                "name": c.name,
                "slug": c.slug,
                "iso": c.iso_code,
                "flag": _flag_emoji(c.iso_code),
                "count": counts.get(c.iso_code, 0),
            }
        )
    ordered, seen = [], set()
    for cont in CONTINENT_ORDER:
        if cont in groups:
            ordered.append({"continent": cont, "landing_url": landing.get(cont), "countries": groups[cont]})
            seen.add(cont)
    for cont in sorted(groups):
        if cont not in seen:
            ordered.append({"continent": cont, "landing_url": landing.get(cont), "countries": groups[cont]})
    return ordered


def _unmatched_works_qs():
    """Works with geometry but no linked country (same set the backfill sweep
    processes — see ``works.tasks.backfill_work_countries``). Works marked "will
    not be matched" carry the sentinel country, so they are already excluded."""
    return (
        Work.objects.filter(geometry__isnull=False)
        .exclude(geometry__isempty=True)
        .filter(countries__isnull=True)
        .only("id", "title", "doi")  # the curation list renders only these
        .order_by("id")
    )


def countries_overview(request):
    """`/countries/` — all countries grouped by continent (mirrors `/regions/`).

    Staff additionally see a collapsible curation section listing works that
    could not be matched to a country (#261).
    """
    if request.user.is_staff:
        return _countries_staff(request)
    return _countries_anonymous(request)


def _countries_context(request):
    page_url = reverse("optimap:countries")
    return {
        "groups": _countries_by_continent(),
        "country_simplification_tolerance": getattr(settings, "COUNTRY_SIMPLIFICATION_TOLERANCE", 0.05),
        "meta": build_facet_page_meta(
            request,
            title="Countries — OPTIMAP",
            description="Browse research works by country, grouped by continent.",
            page_url=page_url,
        ),
        "canonical_url": request.build_absolute_uri(page_url),
    }


@cache_page(settings.PAGE_CACHE_SHORT, cache="memory")
def _countries_anonymous(request):
    return render(request, "countries.html", _countries_context(request))


@never_cache
def _countries_staff(request):
    context = _countries_context(request)
    page_obj, page_size = paginate_works(request, _unmatched_works_qs())
    context.update(
        {
            "page_obj": page_obj,
            "page_size": page_size,
            "page_size_options": settings.WORKS_PAGE_SIZE_OPTIONS,
            "country_options": list(Country.objects.real().values("iso_code", "name").order_by("name")),
        }
    )
    return render(request, "countries.html", context)


@staff_member_required
@require_POST
def set_work_country(request, work_id):
    """Manually assign one or more countries to a work, or mark it "will not be
    matched" (#261).

    Staff-only. Body is JSON: ``{"iso_codes": ["DE", "PL"]}`` (or the
    single-value ``{"iso_code": "DE"}``) to assign — multi-valued for
    transboundary studies — or ``{"exclude": true}`` to mark the work as not
    applicable (assigns the sentinel ``Country``). Records a manual provenance
    block and an audit event; the manual decision is preserved until the work's
    geometry changes (see ``works.signals.assign_work_countries``).
    """
    work = get_object_or_404(Work, pk=work_id)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON body."}, status=400)

    now = timezone.now().isoformat()
    # Resolve the target countries and the decision-specific provenance values;
    # the persistence tail below is shared between assign and exclude.
    if data.get("exclude"):
        sentinel = Country.objects.filter(iso_code=SENTINEL_COUNTRY_ISO).first()
        if sentinel is None:
            return JsonResponse({"success": False, "error": "Sentinel country missing; run migrations."}, status=500)
        decision, method, countries, iso_codes = "excluded", "curator_excluded", [sentinel], []
    else:
        # Accept a list (iso_codes) or the legacy single value (iso_code).
        raw = data.get("iso_codes")
        if raw is None:
            raw = [data["iso_code"]] if data.get("iso_code") else []
        if not isinstance(raw, list):
            return JsonResponse({"success": False, "error": "iso_codes must be a list."}, status=400)
        requested = [c.upper() for c in raw if isinstance(c, str) and c.strip()]
        if not requested:
            return JsonResponse({"success": False, "error": "No country codes provided."}, status=400)
        countries = list(Country.objects.real().filter(iso_code__in=requested))
        found = {c.iso_code for c in countries}
        unknown = [c for c in requested if c not in found]
        if unknown:
            return JsonResponse(
                {"success": False, "error": f"Unknown country code(s): {', '.join(unknown)}."}, status=400
            )
        decision, method = "assigned", "curator_assigned"
        iso_codes = sorted(found)

    work.countries.set(countries)
    # append_event mutates provenance in-memory; set_block then persists the
    # whole dict via .update() without re-firing the save signals.
    append_event(
        work,
        "country_curation",
        user_id=request.user.id,
        user_email=request.user.email,
        decision=decision,
        iso_codes=iso_codes or None,
    )
    set_block(
        work,
        "countries",
        {
            "source": "manual",
            "method": method,
            "iso_codes": iso_codes,
            "decided_by": request.user.id,
            "decided_at": now,
        },
    )
    return JsonResponse({"success": True, "decision": decision, "iso_codes": iso_codes})


@staff_member_required
@require_POST
def trigger_country_backfill(request):
    """Enqueue a one-time background run of the country backfill task (#261).

    Staff-only. Mirrors the data-dump regeneration button: queues
    ``works.tasks.backfill_work_countries`` via Django-Q and returns the task id.
    """
    task_id = async_task("works.tasks.backfill_work_countries", trigger_source="manual")
    task_name = humanize_task_id(task_id)
    logger.info("User %s triggered country backfill (task %s)", request.user, task_name)
    return JsonResponse({"success": True, "task_id": task_id, "task_name": task_name})


def place_index(request):
    """`/at/` — every place you can land on: continents, oceans, and countries."""
    continents = [
        {"name": r.name, "slug": r.get_slug()}
        for r in GlobalRegion.objects.filter(region_type=GlobalRegion.CONTINENT).order_by("name")
    ]
    oceans = [
        {"name": r.name, "slug": r.get_slug()}
        for r in GlobalRegion.objects.filter(region_type=GlobalRegion.OCEAN).order_by("name")
    ]
    page_url = reverse("optimap:at-index")
    meta = build_facet_page_meta(
        request,
        title="Places — OPTIMAP",
        description="Browse research works by place: continents, oceans, and countries.",
        page_url=page_url,
    )
    return render(
        request,
        "place_index.html",
        {
            "continents": continents,
            "oceans": oceans,
            "groups": _countries_by_continent(),
            "meta": meta,
            "canonical_url": request.build_absolute_uri(page_url),
        },
    )


# --- /by/ author pages -------------------------------------------------------


def author_page(request, orcid):
    """Works by an author identified by their bare ORCID iD.

    Matches the canonical bare form (e.g. '0000-0002-1825-0097'). An ORCID
    supplied as a URL (https://orcid.org/...) is normalized and redirected.
    """
    from works.utils.orcid import normalize_orcid

    normalized = normalize_orcid(orcid)
    if normalized is None:
        raise Http404(f"Invalid ORCID: {orcid}")

    # Redirect URL-form ORCIDs to the canonical bare slug.
    if orcid != normalized:
        return redirect(reverse("optimap:by-author", kwargs={"orcid": normalized}), permanent=True)

    author = get_object_or_404(Author, orcid=normalized)
    works = (
        Work.objects.filter(status__in=["p", "h"], author_links=author)
        .select_related("source")
        .order_by("-creationDate", "-id")
        .distinct()
    )
    works = annotate_rounded_geometry(works)
    page_url = reverse("optimap:by-author", kwargs={"orcid": normalized})
    return _render_facet(
        request,
        page_url=page_url,
        heading=f"Works by {author.name}",
        lead="",
        title=f"{author.name} — works on OPTIMAP",
        description=f"Research works by {author.name} with geographic metadata on OPTIMAP.",
        works=works,
        with_map=True,
        map_cache_key=f"facet_map_all:author:{normalized}",
        extra={"author": author},
    )


def author_index(request):
    """Paginated directory of all identity-bearing authors with published works."""
    qs = (
        Author.objects.filter(orcid__isnull=False)
        .annotate(work_count=Count("works", filter=Q(works__status__in=["p", "h"])))
        .filter(work_count__gt=0)
        .order_by("name")
    )
    page_obj, page_size = paginate_works(request, qs)
    page_url = reverse("optimap:by-index")
    meta = build_facet_page_meta(
        request,
        title="People on OPTIMAP — authors with published works",
        description="Browse research authors with published works on OPTIMAP, linked by ORCID iD.",
        page_url=page_url,
    )
    context = {
        "facet_heading": "People",
        "facet_lead": "Authors of published works on OPTIMAP, identified by ORCID iD.",
        "page_obj": page_obj,
        "page_size": page_size,
        "page_size_options": settings.WORKS_PAGE_SIZE_OPTIONS,
        "meta": meta,
        "canonical_url": request.build_absolute_uri(page_url),
    }
    return render(request, "author_index.html", context)
