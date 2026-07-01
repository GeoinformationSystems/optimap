# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, Q
from django.db.models.functions import ExtractYear
from django.utils import timezone

from works.models import SENTINEL_COUNTRY_ISO, Collection, Source, Work

logger = logging.getLogger(__name__)

STATS_CACHE_KEY = "publications_statistics"
STATS_CACHE_TIMEOUT = 86400  # 24 hours


def calculate_statistics():
    """Calculate comprehensive statistics about publications."""
    from django.contrib.auth import get_user_model

    from works.models import Contribution, GlobalRegion

    User = get_user_model()

    published = Work.objects.filter(status="p")

    stats = {
        "total_works": Work.objects.count(),
        "published_works": published.count(),
        "harvested_works": Work.objects.filter(status="h").count(),
        "contributed_works": Work.objects.filter(status="c").count(),
        "contributed_dois": Contribution.objects.filter(kind=Contribution.DOI).count(),
        "with_geometry": published.exclude(geometry__isnull=True).count(),
        "with_temporal": published.filter(
            Q(timeperiod_startdate__isnull=False) | Q(timeperiod_enddate__isnull=False)
        ).count(),
        "with_authors": published.exclude(authors__isnull=True).exclude(authors=[]).count(),
        "with_doi": published.exclude(doi__isnull=True).exclude(doi="").count(),
        "with_abstract": published.exclude(abstract__isnull=True).exclude(abstract="").count(),
        "open_access": published.exclude(openalex_open_access_status__isnull=True)
        .exclude(openalex_open_access_status="")
        .count(),
        "from_openalex": published.exclude(openalex_id__isnull=True).exclude(openalex_id="").count(),
        "works_by_status": {s: Work.objects.filter(status=s).count() for s in ("p", "h", "c", "d", "t", "w")},
        "sources": Source.objects.count(),
        "collections": Collection.objects.count(),
        "users": User.objects.count(),
        "contributors": Contribution.objects.exclude(user__isnull=True).values("user").distinct().count(),
    }

    complete = (
        published.exclude(geometry__isnull=True)
        .filter(Q(timeperiod_startdate__isnull=False) | Q(timeperiod_enddate__isnull=False))
        .exclude(authors__isnull=True)
        .exclude(authors=[])
    )
    stats["with_complete_metadata"] = complete.count()
    stats["complete_percentage"] = (
        round(stats["with_complete_metadata"] / stats["published_works"] * 100, 1)
        if stats["published_works"] > 0
        else 0
    )

    # --- Breakdowns ---

    # by_continent / by_ocean — read the persisted Work.regions M2M (populated by
    # the assign_work_regions signal / backfill_work_regions sweep), counted the
    # same way as by_country. A single grouped query over every region (zero-count
    # regions included) replaces the previous per-region spatial intersection.
    by_continent, by_ocean = [], []
    regions = GlobalRegion.objects.annotate(cnt=Count("works", filter=Q(works__status="p"), distinct=True))
    for region in regions:
        entry = {"name": region.name, "count": region.cnt}
        (by_continent if region.region_type == GlobalRegion.CONTINENT else by_ocean).append(entry)
    stats["by_continent"] = sorted(by_continent, key=lambda x: -x["count"])
    stats["by_ocean"] = sorted(by_ocean, key=lambda x: -x["count"])

    # by_country — Work.countries M2M (ISO 3166-1 alpha-2); a transboundary work
    # counts under each of its countries.
    stats["by_country"] = [
        {"name": row["countries__iso_code"], "count": row["cnt"]}
        for row in (
            published.filter(countries__isnull=False)
            .exclude(countries__iso_code=SENTINEL_COUNTRY_ISO)
            .values("countries__iso_code")
            .annotate(cnt=Count("id", distinct=True))
            .order_by("-cnt")[:100]
        )
    ]

    # by_publisher
    stats["by_publisher"] = [
        {"name": row["source__publisher_name"], "count": row["cnt"]}
        for row in (
            published.exclude(source__isnull=True)
            .exclude(source__publisher_name__isnull=True)
            .exclude(source__publisher_name="")
            .values("source__publisher_name")
            .annotate(cnt=Count("id"))
            .order_by("-cnt")[:50]
        )
    ]

    # by_journal
    stats["by_journal"] = [
        {"name": row["source__name"], "count": row["cnt"]}
        for row in (
            published.exclude(source__isnull=True)
            .values("source__name")
            .annotate(cnt=Count("id"))
            .order_by("-cnt")[:50]
        )
        if row["source__name"]
    ]

    # by_topic / by_year reuse the canonical facet helpers so the statistics
    # page matches the /on/<topic> and /during/<year> facet pages exactly.
    from works.views_indexed import data_year_counts, published_topic_counts

    # by_topic — top 50 research topics (Work.topics, typically OpenAlex),
    # counted per distinct topic a published work carries.
    stats["by_topic"] = [
        {"name": name, "count": count}
        for name, count in sorted(published_topic_counts().items(), key=lambda kv: -kv[1])[:50]
    ]

    # by_year — published works per **data year** (temporal coverage / extent,
    # NOT publication date), ascending chronological. Each work counts under
    # every year its temporal interval(s) span.
    stats["by_year"] = [{"year": year, "count": count} for year, count in sorted(data_year_counts().items())]

    # by_collection — all public collections with their published work counts
    stats["by_collection"] = sorted(
        [
            {
                "name": coll.name,
                "url": coll.get_absolute_url(),
                "count": published.filter(collections=coll).count(),
            }
            for coll in Collection.objects.filter(is_published=True).order_by("name")
        ],
        key=lambda x: -x["count"],
    )

    return stats


def save_statistics_snapshot():
    """Calculate statistics and persist a StatisticsSnapshot row."""
    from works.models import StatisticsSnapshot

    stats = calculate_statistics()
    now = timezone.now()
    snapshot = StatisticsSnapshot.objects.create(
        next_update=now + timedelta(hours=24),
        total_works=stats["total_works"],
        published_works=stats["published_works"],
        harvested_works=stats["harvested_works"],
        contributed_works=stats["contributed_works"],
        contributed_dois=stats["contributed_dois"],
        with_geometry=stats["with_geometry"],
        with_temporal=stats["with_temporal"],
        with_complete_metadata=stats["with_complete_metadata"],
        complete_percentage=stats["complete_percentage"],
        with_authors=stats["with_authors"],
        with_doi=stats["with_doi"],
        with_abstract=stats["with_abstract"],
        open_access=stats["open_access"],
        sources=stats["sources"],
        collections=stats["collections"],
        users=stats["users"],
        contributors=stats["contributors"],
        by_continent=stats["by_continent"],
        by_ocean=stats["by_ocean"],
        by_country=stats["by_country"],
        by_publisher=stats["by_publisher"],
        by_journal=stats["by_journal"],
        by_collection=stats["by_collection"],
        by_topic=stats["by_topic"],
        by_year=stats["by_year"],
    )
    return snapshot


def calculate_source_coverage(source):
    """Compute and persist a SourceCoverageSnapshot for one Source.

    Uses Source.works_count (populated by update_openalex_sources) as the
    OpenAlex total. openalex_total and coverage_pct are NULL when the source
    has no works_count (e.g. no openalex_id), so zero is never used as a
    sentinel for "unknown".
    """
    from django.db.models import Count, Q

    from works.models import Contribution, SourceCoverageSnapshot

    published = Work.objects.filter(status="p", source=source)
    optimap_count = published.count()

    openalex_total = source.works_count  # None when not set
    coverage_pct = round(optimap_count / openalex_total * 100, 1) if openalex_total else None

    def _rate(numerator):
        return round(numerator / optimap_count * 100, 1) if optimap_count > 0 else None

    spatial_rate = _rate(published.exclude(geometry__isnull=True).count())
    temporal_rate = _rate(
        published.filter(Q(timeperiod_startdate__isnull=False) | Q(timeperiod_enddate__isnull=False)).count()
    )
    open_access_ratio = _rate(published.filter(openalex_open_access_status__in=("gold", "green", "hybrid")).count())
    contributors_count = (
        Contribution.objects.filter(work__source=source).exclude(user__isnull=True).values("user").distinct().count()
    )
    by_year = [
        {"year": row["year"], "count": row["cnt"]}
        for row in (
            published.exclude(publicationDate__isnull=True)
            .annotate(year=ExtractYear("publicationDate"))
            .values("year")
            .annotate(cnt=Count("id"))
            .order_by("year")
        )
    ]

    return SourceCoverageSnapshot.objects.create(
        source=source,
        openalex_total=openalex_total,
        optimap_count=optimap_count,
        coverage_pct=coverage_pct,
        spatial_rate=spatial_rate,
        temporal_rate=temporal_rate,
        open_access_ratio=open_access_ratio,
        contributors_count=contributors_count,
        by_year=by_year,
    )


def get_cached_statistics():
    """Return statistics from cache, calculating if absent."""
    stats = cache.get(STATS_CACHE_KEY)
    if stats is None:
        stats = calculate_statistics()
        cache.set(STATS_CACHE_KEY, stats, STATS_CACHE_TIMEOUT)
    return stats


def update_statistics_cache():
    """Force recalculation and refresh the cache."""
    stats = calculate_statistics()
    cache.set(STATS_CACHE_KEY, stats, STATS_CACHE_TIMEOUT)
    return stats


def clear_statistics_cache():
    """Clear the statistics cache."""
    cache.delete(STATS_CACHE_KEY)
