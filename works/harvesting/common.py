# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared helpers used by every harvester (OAI-PMH, RSS, Crossref, MaRESS).

This module contains:
- HarvestStats and HarvestWarningCollector — accumulators threaded through parsers.
- The dedup / careful-update helpers (`_save_or_update_work` and friends).
- Small utilities used by parsers: `parse_publication_date`, `_get_article_link`,
  `get_or_create_admin_command_user`.
- Completion / failure / notification helpers that replace the previously
  duplicated success/failure/email blocks at the bottom of each harvester.
"""

import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.utils import timezone
from django_q.tasks import async_task

from works.models import Work

logger = logging.getLogger(__name__)
User = get_user_model()


class HarvestStats:
    """Tally of `_save_or_update_work` outcomes for one harvest run.

    Each parser increments the matching counter at the call site. The harvester
    then persists `created` to `HarvestingEvent.records_added`, `updated` to
    `records_updated`, and uses the totals in the completion email.
    """

    __slots__ = (
        "created",
        "updated",
        "doi_backfilled",
        "skipped_same_source",
        "skipped_cross_source",
        "skipped_existing",
        "skipped_no_geometry",
    )

    def __init__(self):
        self.created = 0
        self.updated = 0
        self.doi_backfilled = 0
        self.skipped_same_source = 0
        self.skipped_cross_source = 0
        self.skipped_existing = 0
        # Records fetched but not ingested because they carry no usable geometry
        # (e.g. IGSN samples with a place name but no coordinates). Kept distinct
        # from dedup skips so logs/summaries don't misattribute them.
        self.skipped_no_geometry = 0

    @property
    def skipped(self):
        return self.skipped_same_source + self.skipped_cross_source + self.skipped_existing + self.skipped_no_geometry

    def record(self, action):
        if action == "created":
            self.created += 1
        elif action == "updated":
            self.updated += 1
        elif action == "doi_backfilled":
            self.doi_backfilled += 1
        elif action == "skipped_same_source":
            self.skipped_same_source += 1
        elif action == "skipped_cross_source":
            self.skipped_cross_source += 1
        elif action == "skipped_existing":
            self.skipped_existing += 1
        elif action == "skipped_no_geometry":
            self.skipped_no_geometry += 1


class HarvestWarningCollector(logging.Handler):
    """
    Custom logging handler to collect warning and error messages during harvesting.

    This handler collects messages for email summaries while also ensuring they are
    logged to the standard output/error through the normal logging chain.

    Categorizes messages with emoji severity indicators:
    - 🔴 ERROR: Critical errors that prevented processing
    - 🟡 WARNING: Issues that didn't prevent processing but need attention
    - 🔵 INFO: Important informational messages
    """

    def __init__(self, passthrough=True):
        super().__init__()
        self.warnings = []
        self.errors = []
        self.info = []
        self.passthrough = passthrough

    def emit(self, record):
        message = self.format(record)
        if record.levelno >= logging.ERROR:
            entry = f"🔴 ERROR: {message}"
            if entry not in self.errors:
                self.errors.append(entry)
        elif record.levelno >= logging.WARNING:
            entry = f"🟡 WARNING: {message}"
            if entry not in self.warnings:
                self.warnings.append(entry)
        elif record.levelno >= logging.INFO and any(
            keyword in message.lower() for keyword in ["no openalex match", "openalex matching failed", "skipping"]
        ):
            self.info.append(f"🔵 INFO: {message}")

    def add_warning(self, message):
        """Record a warning directly, independent of the logging pipeline.

        Use this for messages that *must* land in ``HarvestingEvent.log_text``
        (e.g. "harvest stopped early") even when global logging state has been
        altered (``logging.disable``, a raised logger level, a disabled logger)
        — which would otherwise drop the matching ``logger.warning`` call before
        it reaches :meth:`emit`. Deduplicated against entries already captured
        via the logging handler so a message logged *and* recorded appears once.
        """
        entry = f"🟡 WARNING: {message}"
        if entry not in self.warnings:
            self.warnings.append(entry)

    def get_summary(self):
        """Return a formatted summary of all collected messages."""
        summary_parts = []

        if self.errors:
            summary_parts.append(f"\n{'=' * 70}\n🔴 ERRORS ({len(self.errors)})\n{'=' * 70}")
            summary_parts.extend(self.errors)

        if self.warnings:
            summary_parts.append(f"\n{'=' * 70}\n🟡 WARNINGS ({len(self.warnings)})\n{'=' * 70}")
            summary_parts.extend(self.warnings)

        if self.info:
            summary_parts.append(f"\n{'=' * 70}\n🔵 NOTABLE INFORMATION ({len(self.info)})\n{'=' * 70}")
            summary_parts.extend(self.info)

        if not (self.errors or self.warnings or self.info):
            return "\n✅ No warnings or errors during harvesting!"

        return "\n".join(summary_parts)

    def has_issues(self):
        return bool(self.errors or self.warnings or self.info)


def get_or_create_admin_command_user():
    """Get or create the system user that owns harvested publications."""
    username = "django_admin_command"
    email = "django_admin_command@system.local"
    user, created = User.objects.get_or_create(
        username=username,
        defaults={
            "email": email,
            "is_active": False,
            "is_staff": False,
        },
    )
    if created:
        logger.info("Created system user: %s", username)
    return user


def ensure_collection_for_source(source):
    """Make sure ``source.collection`` is set, creating a Collection on first
    harvest if needed.

    Called from every harvest entry point (OAI-PMH / OJS / Janeway, the
    Mountain Wetlands API, OpenAlex-as-source). Issue #192 originally asked
    the generic OAI-PMH harvester to "create a collection for each endpoint
    based on the provided metadata"; the same auto-creation now applies to
    the other harvesters so admins don't have to pre-seed collections via
    fixtures or the admin UI. The Collection is keyed by a slug derived from
    the source's name (with a numeric suffix if the slug already exists under
    a different source) and starts ``is_published=False`` so admins can
    review name/description before exposing it on ``/collections/``.

    Returns the (possibly newly-created) ``Collection``, or ``None`` when the
    source has no usable name to derive a slug from. No-op when
    ``source.collection`` is already set.
    """
    # Avoid a circular import: works.models imports works.harvesting via tasks.
    from django.utils.text import slugify

    from works.models import Collection

    if source.collection_id is not None:
        return source.collection
    base_name = (source.name or "").strip()
    if not base_name:
        logger.warning(
            "Source id=%s has no name — cannot auto-create a Collection.",
            source.id,
        )
        return None
    base_slug = slugify(base_name)[:100] or f"source-{source.id}"
    identifier = base_slug
    suffix = 2
    while Collection.objects.filter(identifier=identifier).exists():
        identifier = f"{base_slug}-{suffix}"[:100]
        suffix += 1
    collection = Collection.objects.create(
        identifier=identifier,
        name=base_name,
        description="",
        homepage_url=source.homepage_url or None,
        # Start unpublished — admins curate name/description and flip the
        # toggle when the collection is ready to be public.
        is_published=False,
    )
    source.collection = collection
    source.save(update_fields=["collection"])
    logger.info(
        "Auto-created Collection %r (id=%s) for source id=%s (%s)",
        identifier,
        collection.id,
        source.id,
        base_name,
    )
    return collection


def _get_article_link(work):
    """Prefer our site permalink if DOI exists, else fallback to original URL."""
    if getattr(work, "doi", None):
        base = settings.BASE_URL.rstrip("/")
        return f"{base}/work/{work.doi}"
    return work.url


def parse_publication_date(date_string):
    """Normalise mixed date strings to YYYY-MM-DD; falls back to Jan 1 of any year."""
    if not date_string:
        return None
    date_string = date_string.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_string):
        return date_string
    if re.match(r"^\d{4}-\d{2}$", date_string):
        return f"{date_string}-01"
    if re.match(r"^\d{4}$", date_string):
        return f"{date_string}-01-01"
    year_match = re.search(r"\b(\d{4})\b", date_string)
    if year_match:
        return f"{year_match.group(1)}-01-01"
    logger.warning("Could not parse date format: %s", date_string)
    return None


# -----------------------------------------------------------------------------
# Harvest dedup + careful-update helpers (used by every harvester).
#
# Dedup is per-source: a Work that already exists *under the same Source* is
# the trigger for either skipping or updating. A Work that exists under a
# *different* Source is logged and skipped — OPTIMAP does not currently
# attempt to merge metadata across sources for the same article (see
# docs/manage.md "Deduplication and updates"). Without per-source scoping, the
# Source.{url,doi} model uniqueness would still catch the second insert at
# the DB level, but with an IntegrityError; the explicit pre-check is
# cleaner and lets the caller distinguish same-source vs cross-source.
#
# update_existing=True opts a harvester into in-place updates instead of
# skipping same-source duplicates. The update is *careful*:
#   - geometry / timeperiod_startdate / timeperiod_enddate are preserved
#     if the new harvest delivers nothing for them (the existing values
#     may be user contributions that the source still does not provide);
#   - status and created_by are never overwritten (curation state and
#     audit provenance must survive re-harvest);
#   - provenance.harvest / metadata_sources / openalex_match are refreshed
#     from the new harvest, but provenance.events is preserved and gets
#     a new "harvest_update" entry appended.
# -----------------------------------------------------------------------------

# geometry / dates may be user contributions the source still lacks; abstract /
# keywords / authors may have been filled by an enrichment source (OpenAIRE,
# OpenAlex) that the harvest origin does not provide — a re-harvest that brings
# nothing (None) for these must not wipe the enriched value. (Crossref returns
# abstract=None / authors=None when absent, which `_is_empty_for_update` treats
# as empty, so the existing value is kept.)
_HARVEST_PRESERVE_IF_NEW_EMPTY = (
    "geometry",
    "timeperiod_startdate",
    "timeperiod_enddate",
    "abstract",
    "keywords",
    "authors",
    # OpenAlex locations may have been filled by an enrichment pass that a
    # plain re-harvest does not carry — don't blank them with an empty list.
    "locations",
)
_HARVEST_NEVER_OVERWRITE = ("status", "created_by", "creationDate")


def _is_empty_for_update(value):
    """True when a freshly harvested field value carries no information that
    should overwrite an existing one."""
    if value is None:
        return True
    if isinstance(value, list) and not value:
        return True
    if hasattr(value, "empty") and getattr(value, "empty"):
        return True
    return False


def _find_existing_work(doi=None, url=None):
    """Return any Work matching ``doi`` or ``url`` (regardless of source)."""
    if doi:
        existing = Work.objects.filter(doi=doi).first()
        if existing:
            return existing
    if url:
        existing = Work.objects.filter(url=url).first()
        if existing:
            return existing
    return None


def _carefully_update_work(work, new_fields, event):
    """Update ``work`` in place from re-harvested ``new_fields``."""
    new_provenance = new_fields.pop("provenance", None)

    for field, new_value in new_fields.items():
        if field in _HARVEST_NEVER_OVERWRITE:
            continue
        if field in _HARVEST_PRESERVE_IF_NEW_EMPTY and _is_empty_for_update(new_value):
            continue
        setattr(work, field, new_value)

    existing_provenance = work.provenance if isinstance(work.provenance, dict) else {}
    if isinstance(new_provenance, dict):
        for key in ("harvest", "metadata_sources", "openalex_match"):
            if key in new_provenance:
                existing_provenance[key] = new_provenance[key]
    existing_provenance.setdefault("events", []).append(
        {
            "type": "harvest_update",
            "at": timezone.now().isoformat(),
            "harvesting_event_id": event.id if event else None,
        }
    )
    work.provenance = existing_provenance

    work.job = event
    work.save()
    return work


def _backfill_empty_doi(work, new_doi, event):
    """Populate an existing work's empty ``doi`` from a re-harvest.

    Some legacy works (notably AGILE-GISS records harvested via the old
    Copernicus OAI-PMH endpoint, which sometimes did not carry the DOI in
    ``dc:identifier`` / ``dc:relation``) ended up with ``doi=None``. When a
    later re-harvest from a richer source (Crossref, OpenAlex) finds the
    same work by URL it would otherwise skip — see the source-match /
    ``update_existing`` branches below — and the DOI gap would persist.

    This helper is the targeted exception: when the existing record has no
    DOI and the new harvest has one, write just the DOI (and bump
    ``lastUpdate`` and ``provenance.events``) regardless of source identity
    or ``update_existing``.
    """
    work.doi = new_doi
    existing_provenance = work.provenance if isinstance(work.provenance, dict) else {}
    existing_provenance.setdefault("events", []).append(
        {
            "type": "doi_backfill",
            "at": timezone.now().isoformat(),
            "doi": new_doi,
            "harvesting_event_id": event.id if event else None,
        }
    )
    work.provenance = existing_provenance
    # Include lastUpdate explicitly: with update_fields, auto_now fields are
    # not bumped automatically, and we want the work-landing cache key to
    # invalidate so the freshly populated DOI shows up immediately.
    work.save(update_fields=["doi", "provenance", "lastUpdate"])
    logger.info("Backfilled empty DOI on work id=%s with %s", work.id, new_doi)


def _save_or_update_work(work_kwargs, source, event, update_existing=False):
    """Create or update a Work, applying per-source dedup.

    Returns ``(work_or_none, action)`` where ``action`` is one of:
      * ``'created'`` — new Work was inserted,
      * ``'updated'`` — existing same-source Work was updated in place,
      * ``'doi_backfilled'`` — existing work had no DOI; only ``doi`` was filled in,
      * ``'skipped_same_source'`` — same-source duplicate, ``update_existing`` was False,
      * ``'skipped_cross_source'`` — different-source duplicate (never auto-merged).
    """
    doi_value = work_kwargs.get("doi")
    url_value = work_kwargs.get("url")

    existing = _find_existing_work(doi=doi_value, url=url_value)
    if existing is not None:
        # Targeted DOI backfill runs *before* the source-match / skip logic
        # so a legacy no-DOI work gets its DOI populated even from a
        # different-source re-harvest.
        backfilled = bool(doi_value) and not existing.doi
        if backfilled:
            _backfill_empty_doi(existing, doi_value, event)

        if existing.source_id != getattr(source, "id", None):
            logger.info(
                "Skipping cross-source duplicate %s — already harvested under source id=%s",
                doi_value or url_value,
                existing.source_id,
            )
            return existing, "doi_backfilled" if backfilled else "skipped_cross_source"
        if not update_existing:
            logger.debug(
                "Skipping same-source duplicate %s (use --update / update_existing=True to refresh)",
                doi_value or url_value,
            )
            return existing, "doi_backfilled" if backfilled else "skipped_same_source"
        _carefully_update_work(existing, work_kwargs, event)
        _reconcile_dedup(existing)
        return existing, "updated"

    work = Work.objects.create(**work_kwargs)
    _reconcile_dedup(work)
    return work, "created"


def _reconcile_dedup(work):
    """Auto-merge duplicate siblings after a harvest save.

    Two independent dedup bases run here: same-``openalex_id`` cross-source
    duplicates, and ESSOAr/Authorea per-version DOIs (``…/v2``) that share a
    versionless base. The version pass runs regardless of ``openalex_id`` (most
    versioned preprints never get an OpenAlex match). Imported lazily to avoid a
    circular import (``works.dedup`` imports the harvesting helpers). Failures
    must never break a harvest, so they are logged and swallowed — the scheduled
    ``dedup_sweep`` will retry.
    """
    try:
        from works.dedup import reconcile, reconcile_versions

        if getattr(work, "openalex_id", None):
            work = reconcile(work) or work
        reconcile_versions(work)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Dedup reconcile failed for work id=%s: %s", getattr(work, "id", None), exc)


# -----------------------------------------------------------------------------
# Completion / failure / notify helpers — replace the previously duplicated
# success and failure tail blocks across all four harvesters.
# -----------------------------------------------------------------------------


def resolve_user(user):
    """Admin actions enqueue with a user id rather than a pickled User instance."""
    if isinstance(user, int):
        return User.objects.filter(pk=user).first()
    return user


def start_harvesting_event(source, event_id=None):
    """Return the ``HarvestingEvent`` to harvest into, set to ``in_progress``.

    When ``event_id`` is given (the ``--async`` route in ``harvest_sources``
    pre-creates a ``pending`` event so its PK can be printed and matched in the
    Django admin), reuse that row instead of creating a fresh one — but only if
    it still belongs to ``source`` and hasn't already run. Otherwise (recurring
    schedules, synchronous CLI, admin actions) create a new event as before.
    """
    from works.models import HarvestingEvent  # local import avoids circular import

    if event_id is not None:
        event = HarvestingEvent.objects.filter(id=event_id, source=source).first()
        if event is not None and event.status in ("pending", "in_progress"):
            if event.status != "in_progress":
                event.status = "in_progress"
                event.save(update_fields=["status"])
            return event
    return HarvestingEvent.objects.create(source=source, status="in_progress")


def count_spatial_temporal(event):
    """Return ``(spatial_count, temporal_count)`` for works attached to ``event``."""
    spatial = Work.objects.filter(job=event).exclude(geometry__isnull=True).count()
    temporal = (
        Work.objects.filter(job=event)
        .exclude(timeperiod_startdate__isnull=True)
        .exclude(timeperiod_startdate=[])
        .count()
    )
    return spatial, temporal


_OPENALEX_SOURCES_URL = "https://api.openalex.org/sources/{source_id}"
_OPENALEX_SOURCE_STATS_TIMEOUT = 15

_OAI_TYPES = frozenset(("oai-pmh", "ojs", "janeway"))
_OAI_STATS_TIMEOUT = 30
_OAI_NS = {"oai": "http://www.openarchives.org/OAI/2.0/"}

_CROSSREF_WORKS_URL = "https://api.crossref.org/works"
_CROSSREF_STATS_TIMEOUT = 60  # matches _crossref_session() default; broad-prefix queries can be slow
_CROSSREF_TYPES = frozenset(("crossref-prefix",))


def fetch_and_store_openalex_source_stats(source) -> str | None:
    """Fetch `works_count` from OpenAlex for *source* and persist it in ``source.statistics``.

    No-op (returns ``None``) when ``source`` is ``None`` or has no ``openalex_id``.
    Network errors are logged at WARNING level and swallowed so a stats-fetch
    failure never aborts a harvest.

    Returns a one-line human-readable summary of what happened (suitable for
    appending to a ``HarvestingEvent.log_text``) or ``None`` when no action
    was taken.
    """
    if not source or not source.openalex_id:
        return None
    raw_id = source.openalex_id.strip().rstrip("/")
    source_id = raw_id.rsplit("/", 1)[-1] if "/" in raw_id else raw_id
    if not source_id:
        return None
    url = _OPENALEX_SOURCES_URL.format(source_id=source_id)
    try:
        resp = requests.get(
            url,
            timeout=_OPENALEX_SOURCE_STATS_TIMEOUT,
            headers={"User-Agent": settings.OPTIMAP_USER_AGENT, "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        msg = f"Could not fetch OpenAlex source stats for {source.name} ({source_id}): {exc}"
        logger.info(msg)
        return f"🟡 WARNING: OpenAlex stats — {msg}"

    works_count = data.get("works_count")
    if works_count is None:
        return None

    from works.models import Source  # local import avoids circular import at module level

    current_stats = Source.objects.filter(pk=source.pk).values_list("statistics", flat=True).first() or {}
    fetched_at = timezone.now()
    updated = {**current_stats, "openalex_works_count": works_count, "openalex_fetched_at": fetched_at.isoformat()}
    Source.objects.filter(pk=source.pk).update(statistics=updated)
    logger.info("Stored OpenAlex works_count=%d for source %s", works_count, source.name)
    return (
        f"🔵 INFO: OpenAlex stats — {source.name}: "
        f"{works_count:,} total works in OpenAlex (fetched {fetched_at:%Y-%m-%d})"
    )


def fetch_and_store_oai_works_count(source) -> str | None:
    """Fetch the total record count from an OAI-PMH endpoint via ``ListIdentifiers``
    and persist it in ``source.statistics`` as ``oai_works_count``.

    Uses the ``completeListSize`` attribute of the ``<resumptionToken>`` element
    from the first response page — a single lightweight request, no pagination.
    Falls back to counting ``<header>`` elements when all records fit in one page.

    Uses the same bot-protection-aware session as the OAI harvester so that
    endpoints behind Cloudflare / BunkerWeb PoW challenges (e.g. GEO-LEO) are
    handled correctly.

    No-op for non-OAI source types or when ``source`` is ``None``.
    Network / parse errors are logged at INFO and swallowed.
    """
    if not source or source.source_type not in _OAI_TYPES:
        return None

    parsed = urlparse(source.url_field or "")
    if not parsed.scheme:
        return None

    orig_params = parse_qs(parsed.query, keep_blank_values=False)
    new_params: dict = {"verb": "ListIdentifiers", "metadataPrefix": "oai_dc"}
    if "set" in orig_params:
        new_params["set"] = orig_params["set"][0]

    url = urlunparse(parsed._replace(query=urlencode(new_params)))

    from works.harvesting.sessions import (  # local import avoids circular at module level
        _oai_session,
        _try_solve_pow_challenge,
    )

    session = _oai_session()
    try:
        resp = session.get(url, timeout=_OAI_STATS_TIMEOUT)
        if resp.status_code == 403 and _try_solve_pow_challenge(session, resp):
            resp = session.get(url, timeout=_OAI_STATS_TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:  # noqa: BLE001
        msg = f"Could not fetch OAI-PMH record count for {source.name}: {exc}"
        logger.info(msg)
        return f"🟡 WARNING: OAI stats — {msg}"

    token = root.find(".//oai:resumptionToken", _OAI_NS)
    if token is not None:
        size_str = token.get("completeListSize")
        if not size_str:
            return None
        try:
            count = int(size_str)
        except ValueError:
            return None
    else:
        # All records fit in one page — count the headers directly.
        headers = root.findall(".//oai:header", _OAI_NS)
        count = len(headers)
        if count == 0:
            return None

    from works.models import Source  # local import avoids circular import at module level

    current_stats = Source.objects.filter(pk=source.pk).values_list("statistics", flat=True).first() or {}
    fetched_at = timezone.now()
    updated = {**current_stats, "oai_works_count": count, "oai_fetched_at": fetched_at.isoformat()}
    Source.objects.filter(pk=source.pk).update(statistics=updated)
    logger.info("Stored OAI works_count=%d for source %s", count, source.name)
    return (
        f"🔵 INFO: OAI stats — {source.name}: "
        f"{count:,} total records in OAI-PMH endpoint (fetched {fetched_at:%Y-%m-%d})"
    )


def fetch_and_store_crossref_works_count(source) -> str | None:
    """Fetch total works count from Crossref for a ``crossref-prefix`` source.

    Builds a ``filter=prefix:{doi_prefix}`` query (plus optional
    ``container-title`` clauses from ``source.source_titles``) with ``rows=0``
    so Crossref returns only the ``message.total-results`` count — a single
    lightweight request.

    No-op for non-Crossref source types, sources without ``doi_prefix``, or
    when ``source`` is ``None``. Network errors are logged at INFO and swallowed.
    """
    if not source or source.source_type not in _CROSSREF_TYPES:
        return None

    raw_filter = (getattr(source, "crossref_filter", "") or "").strip()
    if raw_filter:
        # Venue spans several DOI prefixes (e.g. ESSOAr) — count the raw filter
        # slice. With a doi_contains narrowing the stored count is the whole
        # slice, not the matched subset (Crossref can't count by DOI substring).
        filter_str = raw_filter
    else:
        doi_prefix = getattr(source, "doi_prefix", None)
        if not doi_prefix:
            return None
        filter_parts = [f"prefix:{doi_prefix}"]
        for title in getattr(source, "source_titles", None) or []:
            filter_parts.append(f"container-title:{title}")
        filter_str = ",".join(filter_parts)

    from works.harvesting.sessions import _crossref_session  # local import avoids circular

    session = _crossref_session()
    try:
        resp = session.get(
            _CROSSREF_WORKS_URL,
            params={"filter": filter_str, "rows": "0"},
            timeout=_CROSSREF_STATS_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        msg = f"Could not fetch Crossref works count for {source.name}: {exc}"
        logger.info(msg)
        return f"🟡 WARNING: Crossref stats — {msg}"

    count = (data.get("message") or {}).get("total-results")
    if count is None:
        return None

    from works.models import Source  # local import avoids circular import at module level

    current_stats = Source.objects.filter(pk=source.pk).values_list("statistics", flat=True).first() or {}
    fetched_at = timezone.now()
    updated = {**current_stats, "crossref_works_count": count, "crossref_fetched_at": fetched_at.isoformat()}
    Source.objects.filter(pk=source.pk).update(statistics=updated)
    logger.info("Stored Crossref works_count=%d for source %s", count, source.name)
    return (
        f"🔵 INFO: Crossref stats — {source.name}: {count:,} total works in Crossref (fetched {fetched_at:%Y-%m-%d})"
    )


def complete_harvest(event, stats, warning_collector, spatial_count=None, temporal_count=None):
    """Stamp success-state counters and timestamps on a HarvestingEvent.

    ``spatial_count`` and ``temporal_count`` default to the standard
    ``count_spatial_temporal`` query; harvesters with non-standard semantics
    (e.g. MWR, where every work has a non-null geometry but it may be an
    empty GeometryCollection) can pass pre-computed values.

    Returns ``(spatial_count, temporal_count)`` so callers can include them
    in the harvest-summary email body.
    """
    if spatial_count is None or temporal_count is None:
        s, t = count_spatial_temporal(event)
        if spatial_count is None:
            spatial_count = s
        if temporal_count is None:
            temporal_count = t
    event.status = "completed"
    event.completed_at = timezone.now()
    event.records_added = stats.created
    event.records_updated = stats.updated
    event.records_skipped = stats.skipped
    event.records_with_spatial = spatial_count
    event.records_with_temporal = temporal_count
    event.log_text = warning_collector.get_summary()
    event.save()
    source = event.source if event else None
    stat_lines = list(
        filter(
            None,
            [
                fetch_and_store_openalex_source_stats(source),
                fetch_and_store_oai_works_count(source),
                fetch_and_store_crossref_works_count(source),
            ],
        )
    )
    if stat_lines:
        event.log_text = (event.log_text or "") + "\n\n" + "\n".join(stat_lines)
        event.save(update_fields=["log_text"])

    # Enqueue the OpenAIRE enrichment sweep for this event's works (all sources).
    # Async so harvest speed is decoupled from OpenAIRE's rate limit; the sweep
    # only touches works that have a DOI and are missing a target field.
    if getattr(settings, "OPTIMAP_OPENAIRE_ENRICH_ON_HARVEST", True) and event is not None:
        try:
            from works.harvesting.openaire import openaire_task_q_options

            async_task(
                "works.harvesting.openaire.enrich_event_from_openaire",
                event.id,
                q_options=openaire_task_q_options(),
            )
        except Exception as exc:
            logger.warning("Could not enqueue OpenAIRE enrichment sweep for event %s: %s", event.id, exc)

    return spatial_count, temporal_count


def fail_harvest(event, exc, warning_collector):
    """Stamp failure state on a HarvestingEvent and persist the log + error message."""
    event.status = "failed"
    event.completed_at = timezone.now()
    event.error_message = str(exc)[:1000]
    event.log_text = warning_collector.get_summary()
    event.save()


def send_harvest_email(user, subject, body, fail_silently=False):
    """Guarded send_mail. No-op when the user has no email."""
    if not user or not user.email:
        return
    try:
        send_mail(
            subject,
            body,
            settings.EMAIL_HOST_USER,
            [user.email],
            fail_silently=fail_silently,
        )
    except Exception as e:  # noqa: BLE001 — email failure must not crash the harvest
        logger.error("Failed to send harvest email to %s: %s", user.email, e)


def render_harvest_email(template_name, context):
    """Render a harvest email template and split subject from body.

    Returns ``(subject, body)``. Delegates to ``works.utils.email.render_email``
    so autoescape is off (plain-text output — no HTML entities in URLs, etc.).
    """
    from works.utils.email import render_email

    return render_email(template_name, context)
