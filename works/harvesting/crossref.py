# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Crossref-prefix harvester.

Enumerates a publisher's output from the Crossref REST API by DOI prefix
(optionally narrowed by container title). This is the primary harvest route
for Copernicus Publications (DOI prefix 10.5194, publisher = "Copernicus
GmbH"): the OAI-PMH endpoint at https://oai-pmh.copernicus.org/oai.php went
404 sometime between the 2025-12-15 Wayback snapshot and 2026-04-29 and has
not recovered, so Crossref is now the established source rather than a
stop-gap. The same harvester also backs Scientific Data (10.1038) and the
AGILE GIScience Series (10.5194).

Trade-off on abstracts: Crossref supplies <jats:p> abstracts that are
usually OK, but the publisher-side article landing pages serve the canonical,
fully-punctuated abstract. This task fetches abstracts directly from the
journal subdomain by default, falling back to the Crossref payload only when
the landing-page fetch fails.
"""

import logging
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

from works.models import HarvestingEvent, Source
from works.utils.provenance import append_event, work_has_contribution_kind

from .bok_pdf import agile_giss_doi_to_pdf_url, extract_bok_from_agile_pdf
from .common import (
    HarvestStats,
    HarvestWarningCollector,
    _carefully_update_work,
    _is_empty_for_update,
    _save_or_update_work,
    complete_harvest,
    ensure_collection_for_source,
    fail_harvest,
    log_work_enriched,
    log_work_failed,
    log_work_retrieved,
    render_harvest_email,
    resolve_user,
    send_harvest_email,
    start_harvesting_event,
)
from .metadata_html import extract_geometry_from_html, extract_timeperiod_from_html
from .openaire import enrich_work_from_openaire
from .openalex import build_openalex_fields, link_openalex_authors
from .sessions import (
    CROSSREF_API_URL,
    CROSSREF_HTTP_TIMEOUT,
    CROSSREF_PAGE_ROWS,
    _crossref_session,
    fetch_with_deadline,
)

# Wall-clock ceiling for a single landing-page fetch (see fetch_with_deadline
# in sessions.py) — requests' own timeout= only bounds the connect and the gap
# between reads, not the call as a whole, so a slowly-trickling server could
# otherwise occupy a worker for far longer than intended.
_LANDING_PAGE_FETCH_DEADLINE = CROSSREF_HTTP_TIMEOUT * 2

#: Stable name for the auto-seeded Source that owns all user-submitted DOIs
#: (the /contribute/ "add a work by DOI" form).
USER_CONTRIBUTIONS_SOURCE_NAME = "User contributions"


def user_contributions_source_url():
    """Display-only ``url_field`` for the User contributions source.

    A ``crossref-prefix`` source's ``url_field`` is never used for harvesting
    (the harvester reads ``doi_prefix`` / ``crossref_filter``), so this is purely
    cosmetic — point it at *this* deployment's own /contribute/ page rather than a
    hardcoded domain, so it is correct regardless of where OPTIMAP is hosted.
    """
    return f"{settings.BASE_URL.rstrip('/')}/contribute/"


logger = logging.getLogger(__name__)


def _strip_jats(jats_html):
    """Strip JATS XML tags from a Crossref abstract.

    Crossref returns abstracts wrapped in <jats:p>, with optional
    <jats:italic>, <jats:sub>, etc. inline. We just want the plain text.
    """
    if not jats_html:
        return None
    soup = BeautifulSoup(jats_html, "html.parser")
    return soup.get_text(separator=" ", strip=True) or None


def _build_crossref_filter(
    prefix, source_titles=None, since=None, extra_filters=None, created_from=None, created_until=None, issn=None
):
    """Assemble a Crossref ``filter=...`` parameter value.

    ``prefix`` may be ``None`` (or empty) to omit the ``prefix:`` clause — used
    when the base query is expressed via ``extra_filters`` instead (e.g.
    ``member:311,type:posted-content`` for ESS Open Archive, whose two DOI eras
    share no single prefix).

    ``created_from`` / ``created_until`` add ``from-created-date`` /
    ``until-created-date`` clauses (the deposit date, which is *stable* per
    record, unlike the constantly-changing index date). They partition a full
    backfill into bounded windows so a single cursor walk never has to span the
    whole ~94k-record member slice — see ``harvest_crossref_prefix``.
    """
    parts = [f"prefix:{prefix}"] if prefix else []
    if source_titles:
        # Crossref lets the same filter key repeat — each title becomes its
        # own clause, and Crossref ORs same-key filters. So a multi-title
        # request widens the result set rather than narrowing it.
        for title in source_titles:
            parts.append(f"container-title:{title}")
    if since:
        parts.append(f"from-update-date:{since}")
    if created_from:
        parts.append(f"from-created-date:{created_from}")
    if created_until:
        parts.append(f"until-created-date:{created_until}")
    if issn:
        parts.append(f"issn:{issn}")
    if extra_filters:
        parts.extend(extra_filters)
    return ",".join(parts)


#: Earliest deposit year to window over for shared-member full backfills. ESS
#: Open Archive's earliest preprints date to 2018; 2015 leaves a safe margin and
#: pre-corpus windows return empty quickly.
_CREATED_WINDOW_START_YEAR = 2015


def _created_date_windows(start_year=_CREATED_WINDOW_START_YEAR):
    """Yield ``(from_created_date, until_created_date)`` yearly deposit windows.

    Covers ``start_year-01-01`` through the end of the current year with no gaps
    or overlaps. Deposit (created) date is stable per record, so partitioning a
    full backfill this way splits the corpus into bounded slices — unlike the
    index date, which is refreshed constantly and would pile ~everything into the
    latest window.
    """
    this_year = timezone.now().date().year
    return [(f"{year}-01-01", f"{year}-12-31") for year in range(start_year, this_year + 1)]


def fetch_copernicus_abstract(landing_url, session=None):
    """Fetch the canonical abstract from a Copernicus journal landing page.

    Returns the plain-text abstract or ``None`` on any failure (network,
    parse, missing selector). Failure is logged at INFO so the caller can
    fall back to the Crossref-supplied abstract without aborting the harvest.
    """
    if not landing_url:
        return None
    s = session or _crossref_session()
    try:
        resp = fetch_with_deadline(
            lambda: s.get(landing_url, timeout=CROSSREF_HTTP_TIMEOUT, allow_redirects=True),
            _LANDING_PAGE_FETCH_DEADLINE,
        )
    except (requests.exceptions.RequestException, TimeoutError) as e:
        logger.info("Abstract fetch failed for %s: %s", landing_url, e)
        return None
    if not resp.ok:
        logger.info(
            "Abstract fetch returned HTTP %s for %s",
            resp.status_code,
            landing_url,
        )
        return None
    soup = BeautifulSoup(resp.content, "html.parser")
    div = soup.select_one("div.abstract, div#abstract")
    if div:
        text = div.get_text(separator=" ", strip=True)
        if text.lower().startswith("abstract"):
            text = text[len("abstract") :].lstrip(" .:")
        return text or None
    meta = soup.select_one('meta[name="citation_abstract"]')
    if meta and meta.get("content"):
        return BeautifulSoup(meta["content"], "html.parser").get_text(separator=" ", strip=True) or None
    return None


def _authors_from_crossref(authors):
    """Crossref ``author`` → list of ``"Given Family"`` strings.

    Crossref entries occasionally only have ``family`` (corporate authors) or
    only ``given`` — keep whatever is there rather than dropping the row.
    """
    if not authors:
        return []
    out = []
    for a in authors:
        if not isinstance(a, dict):
            continue
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = a.get("name") or " ".join(p for p in (given, family) if p).strip()
        if name:
            out.append(name)
    return out


def _split_crossref_page(page):
    """``"12-25"`` → ``("12", "25")``; single locator → ``(value, None)``."""
    if not page:
        return None, None
    text = page.strip()
    if not text:
        return None, None
    if "-" in text:
        first, _, last = text.partition("-")
        first = first.strip() or None
        last = last.strip() or None
        return first, last
    return text, None


def _crossref_item_to_work_kwargs(
    item, source, event, fetch_abstract_from_publisher, abstract_session, harvester_name=None
):
    """Convert a Crossref `works` JSON item to ``Work.objects.create`` kwargs.

    Returns ``None`` if the item lacks the minimum identifier (DOI). Abstract
    resolution prefers the publisher landing page (when ``fetch_abstract_
    from_publisher`` is on) and falls back to the Crossref-supplied JATS.
    """
    doi = item.get("DOI")
    if not doi:
        return None

    url = item.get("URL") or f"https://doi.org/{doi}"
    title_list = item.get("title") or []
    title = title_list[0] if title_list else doi
    log_work_retrieved(event, title)

    published = (
        item.get("published-print")
        or item.get("published-online")
        or item.get("published")
        or item.get("issued")
        or {}
    )
    pub_date = None
    parts = (published.get("date-parts") or [[]])[0]
    if parts:
        try:
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else 1
            day = int(parts[2]) if len(parts) > 2 else 1
            pub_date = datetime(year, month, day).date()
        except (TypeError, ValueError):
            pub_date = None

    abstract = None
    if fetch_abstract_from_publisher:
        abstract = fetch_copernicus_abstract(url, session=abstract_session)
    if not abstract:
        abstract = _strip_jats(item.get("abstract"))

    authors = _authors_from_crossref(item.get("author"))
    volume = (item.get("volume") or None) or None
    issue = (item.get("issue") or None) or None
    first_page, last_page = _split_crossref_page(item.get("page"))

    metadata_sources = {"crossref": "doi"}
    if authors:
        metadata_sources["authors"] = "crossref"
    if volume or issue or first_page or last_page:
        metadata_sources["biblio"] = "crossref"

    # OpenAlex enrichment (DOI-matched): adds research topics and the openalex_*
    # identity fields, and fills authors/keywords/biblio that Crossref left empty.
    # Fill-if-empty — Crossref-supplied values always win, and the source's
    # default_work_type is kept rather than OpenAlex's type.
    existing_metadata = {"authors": authors} if authors else {}
    try:
        openalex_fields, oa_provenance = build_openalex_fields(
            title=title,
            doi=doi,
            author=", ".join(authors) if authors else None,
            existing_metadata=existing_metadata,
        )
    except Exception as e:  # noqa: BLE001 — enrichment must never fail a harvest
        logger.info("OpenAlex enrichment failed for %s: %s", doi, e)
        openalex_fields, oa_provenance = {}, {}
    log_work_enriched(event, title, openalex_fields)

    # Crossref wins over OpenAlex for fields Crossref already provided.
    openalex_fields.pop("type", None)
    oa_provenance.pop("type", None)
    if authors:
        openalex_fields.pop("authors", None)
        oa_provenance.pop("authors", None)
    for biblio_key, crossref_value in (
        ("volume", volume),
        ("issue", issue),
        ("first_page", first_page),
        ("last_page", last_page),
    ):
        if crossref_value:
            openalex_fields.pop(biblio_key, None)
            oa_provenance.pop(biblio_key, None)
    metadata_sources.update(oa_provenance)

    return {
        "title": title,
        "abstract": abstract,
        "doi": doi,
        "url": url,
        "publicationDate": pub_date,
        "source": source,
        "job": event,
        "authors": authors or None,
        "volume": volume,
        "issue": issue,
        "first_page": first_page,
        "last_page": last_page,
        "type": (source.default_work_type if source else None) or "article",
        "provenance": {
            "harvest": {
                "harvester": harvester_name or "harvest_crossref_prefix",
                "source_url": "https://api.crossref.org/works",
                "source_type": source.source_type if source else "crossref-prefix",
                "source_name": source.name if source else None,
                "harvested_at": timezone.now().isoformat(),
                "harvesting_event_id": event.id if event else None,
                "doi": doi,
            },
            "metadata_sources": metadata_sources,
        },
        "status": "h",
        **openalex_fields,
    }


def _try_bok_pdf_extraction(work, doi: str, session) -> None:
    """Download the AGILE GISS PDF for *work* and save extracted BoK codes.

    No-op for non-AGILE DOIs or when bok_concepts are already set. All errors
    are caught and logged **here** (rather than relying on the caller's
    per-record catch) so a PDF-extraction failure is never misattributed as
    "failed to persist" the work — the save that matters already succeeded;
    this is a best-effort, post-save enrichment step only.
    """
    try:
        if not agile_giss_doi_to_pdf_url(doi):
            return
        if work.bok_concepts:
            return
        codes = extract_bok_from_agile_pdf(doi, session=session)
        if not codes:
            return
        pdf_url = agile_giss_doi_to_pdf_url(doi)
        work.bok_concepts = codes
        prov = work.provenance if isinstance(work.provenance, dict) else {}
        prov.setdefault("metadata_sources", {})["bok_concepts"] = "pdf_extraction"
        work.provenance = prov
        append_event(
            work,
            "bok_pdf_extract",
            source="pdf",
            pdf_url=pdf_url,
            codes_found=codes,
        )
        work.save(update_fields=["bok_concepts", "provenance"])
        logger.info("BoK PDF extraction: set %s on work %s", codes, work.id)
    except Exception as exc:  # noqa: BLE001 — best-effort enrichment must never fail the harvest
        logger.warning("BoK PDF extraction failed for work id=%s (doi=%s): %s", work.id, doi, exc)


def parse_crossref_response_and_save_works(
    source,
    event,
    prefix,
    source_titles=None,
    fetch_abstract_from_publisher=True,
    max_records=None,
    warning_collector=None,
    update_existing=False,
    stats=None,
    sort=None,
    order=None,
    extra_filters=None,
    harvester_name=None,
    since=None,
    doi_contains=None,
    created_from=None,
    created_until=None,
    issn=None,
):
    """Page through Crossref's ``works`` API and persist matched works.

    Uses cursor-based pagination (``cursor=*`` then echo back), 100 rows per
    page. Stops after ``max_records`` items have been processed (useful for
    smoke tests). Items already present in the DB by DOI are skipped to
    keep the harvest idempotent on re-run.

    ``sort`` / ``order`` map to Crossref's ``sort=`` / ``order=`` query
    parameters (e.g. ``sort='published', order='desc'``). Default is
    Crossref's default (relevance/score), which is fine for a steady-state
    crawl but useless for "most recent first" comparison runs.

    ``extra_filters`` appends raw Crossref filter clauses (e.g.
    ``["isbn:978-3-030-14745-7"]``) after the prefix/title filters.

    ``since`` (a date / ``YYYY-MM-DD`` string) adds a ``from-update-date``
    clause for incremental harvesting — only records Crossref re-indexed on or
    after that date are returned.

    ``doi_contains`` is a case-insensitive substring filter applied client-side:
    items whose DOI does not contain it are skipped. This is the only way to
    isolate a single venue when a DOI prefix is shared (e.g. ``10.22541`` is
    shared by ESS Open Archive ``.../essoar.*`` and Authorea ``.../au.*``).
    """
    session = _crossref_session()
    cursor = "*"
    saved = 0
    seen = 0  # items matching doi_contains (== walked when no filter); drives stats + max_records
    walked = 0  # every item Crossref returned; drives end-of-crawl detection vs total_results
    if stats is None:
        stats = HarvestStats()
    log_interval = 20 if (max_records or 0) <= 100 else 50

    filter_value = _build_crossref_filter(
        prefix,
        source_titles=source_titles,
        since=since,
        extra_filters=extra_filters,
        created_from=created_from,
        created_until=created_until,
        issn=issn,
    )

    # Crossref intermittently returns an empty `items` page (or drops the
    # `next-cursor`) part-way through a deep cursor crawl — server load,
    # rate-limiting, or eventual consistency of the cursor window. Treating
    # that as end-of-results silently truncates the harvest, so we re-request
    # the same cursor a few times before believing the crawl is done. The
    # authoritative end signal is `walked >= total_results` (Crossref echoes
    # `total-results` on every page) — compared against `walked`, not `seen`,
    # because a `doi_contains` filter makes `seen` count only the matched subset.
    total_results = None
    empty_retries = 0
    # A full member+type backfill is ~900 pages over hours; transient empty
    # pages / cursor hiccups are common, so retry generously before believing
    # the crawl is done (each retry backs off 2s × attempt).
    EMPTY_PAGE_RETRIES = 6

    while True:
        params = {
            "filter": filter_value,
            "rows": str(CROSSREF_PAGE_ROWS),
            "cursor": cursor,
            "select": (
                "DOI,title,abstract,published-print,published-online,"
                "published,issued,URL,container-title,publisher,"
                "author,volume,issue,page"
            ),
        }
        if sort:
            params["sort"] = sort
        if order:
            params["order"] = order
        try:
            resp = session.get(CROSSREF_API_URL, params=params, timeout=CROSSREF_HTTP_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Crossref request failed: {e}") from e
        if not resp.ok:
            raise RuntimeError(
                f"Crossref returned HTTP {resp.status_code} for filter {filter_value!r}: {resp.text[:300]}"
            )

        data = resp.json().get("message", {})
        if total_results is None:
            total_results = data.get("total-results")
        items = data.get("items", [])
        if not items:
            # Transient empty page mid-walk? Retry the same cursor before
            # concluding the crawl is finished — but only while Crossref still
            # claims more results than we've seen.
            if total_results and walked < total_results and empty_retries < EMPTY_PAGE_RETRIES:
                empty_retries += 1
                logger.info(
                    "Crossref returned an empty page at %d/%d records; retry %d/%d on the same cursor",
                    walked,
                    total_results,
                    empty_retries,
                    EMPTY_PAGE_RETRIES,
                )
                time.sleep(2 * empty_retries)
                continue
            if total_results and walked < total_results:
                msg = (
                    f"Crossref harvest stopped early: {walked} of {total_results} records fetched "
                    f"(empty page after {EMPTY_PAGE_RETRIES} retries) for filter {filter_value!r}"
                )
                logger.warning(msg)
                if warning_collector is not None:
                    warning_collector.add_warning(msg)
            break
        empty_retries = 0

        for item in items:
            walked += 1
            if doi_contains and doi_contains.lower() not in (item.get("DOI") or "").lower():
                # Shared-prefix record from another venue (e.g. Authorea under
                # 10.22541); not part of this source — skip without counting.
                continue
            seen += 1
            if seen % log_interval == 0:
                suffix = f"/{max_records}" if max_records else ""
                logger.info("Processed %d%s records", seen, suffix)
                if event is not None:
                    event.append_log(
                        f"Processed {seen}{suffix} records ({stats.created} created, {stats.updated} updated)"
                    )
            kwargs = _crossref_item_to_work_kwargs(
                item,
                source,
                event,
                fetch_abstract_from_publisher,
                session,
                harvester_name=harvester_name,
            )
            if not kwargs:
                continue
            try:
                work, action = _save_or_update_work(
                    kwargs,
                    source,
                    event,
                    update_existing=update_existing,
                )
                stats.record(action)
                if action in ("created", "updated"):
                    link_openalex_authors(work, kwargs)
                    if source and source.collection_id:
                        work.collections.add(source.collection_id)
                if action == "created":
                    saved += 1
                    _try_bok_pdf_extraction(work, kwargs.get("doi", ""), session)
            except Exception as e:
                logger.warning(
                    "Failed to persist Crossref work %s: %s",
                    kwargs.get("doi"),
                    e,
                )
                log_work_failed(event, kwargs.get("title") or kwargs.get("doi"), e)
            if max_records and seen >= max_records:
                return saved, seen

        next_cursor = data.get("next-cursor")
        if not next_cursor:
            if total_results and walked < total_results:
                msg = (
                    f"Crossref harvest stopped early: {walked} of {total_results} records fetched "
                    f"(no next-cursor) for filter {filter_value!r}"
                )
                logger.warning(msg)
                if warning_collector is not None:
                    warning_collector.add_warning(msg)
            break
        # Crossref sometimes returns the same cursor string for consecutive pages
        # (observed for prefix:10.1038,container-title:Scientific Data) — the
        # server-side result window still advances, so different items are returned.
        # We must NOT break on cursor equality; rely on empty items or absent
        # next-cursor to detect the true end of results.
        cursor = next_cursor

    return saved, seen


def harvest_crossref_prefix(
    source_id,
    user=None,
    max_records=None,
    source_titles=None,
    prefix=None,
    fetch_abstract_from_publisher=True,
    update_existing=False,
    sort=None,
    order=None,
    full=False,
    since=None,
    event_id=None,
    issn=None,
    trigger_source=None,
):
    """Harvest publications from Crossref by DOI prefix.

    Primary harvest route for Copernicus (DOI prefix 10.5194, OAI-PMH endpoint
    dead since 2025-12); also used for Scientific Data and the AGILE GIScience
    Series.

    By default the harvest is incremental: it only asks Crossref for records
    re-indexed since the last completed harvest of this source (see below).
    Pass ``full=True`` to force a complete backfill (ignore prior events), or
    ``since="YYYY-MM-DD"`` to set an explicit ``from-update-date`` window. The
    two are mutually exclusive; ``full`` wins if both are given.
    """
    user = resolve_user(user)
    source = Source.objects.get(id=source_id)
    event = start_harvesting_event(source, event_id, trigger_source, max_records)
    if event is None:
        return
    event.append_log(f"Starting Crossref harvest for {source.name} (prefix={prefix or source.doi_prefix})")

    warning_collector = HarvestWarningCollector()
    warning_collector.setLevel(logging.INFO)
    logger.addHandler(warning_collector)

    # Auto-derive ISSN from source.issn_l when not supplied by the caller.
    # Used for journals whose title contains a comma — Crossref's filter parser
    # treats commas as clause separators, so container-title: can't match them.
    if issn is None and source:
        issn = source.issn_l or None

    doi_contains = (source.doi_contains or None) if source else None

    # ESS Open Archive and similar venues span more than one DOI prefix (ESSOAr:
    # 10.1002/essoar.* legacy + 10.22541/essoar.* current) but share a Crossref
    # member + work type. ``crossref_filter`` carries that raw base query (e.g.
    # "member:311,type:posted-content"); when set we drop the prefix clause and
    # rely on doi_contains to isolate the venue.
    raw_filter = (source.crossref_filter or "").strip() if source else ""
    if raw_filter:
        resolved_prefix = None
        extra_filters = [c.strip() for c in raw_filter.split(",") if c.strip()]
    else:
        resolved_prefix = prefix or (source.doi_prefix if source else None) or "10.5194"
        extra_filters = None
    filter_label = resolved_prefix or (",".join(extra_filters) if extra_filters else None)

    # Incremental harvesting: only ask Crossref for records re-indexed since the
    # last successful harvest of this source (minus a 2-day overlap buffer to
    # catch late-indexed updates). First run (no prior completed event) → full
    # backfill (since=None). For shared-prefix sources this is essential: it
    # avoids re-walking the entire prefix (~79k for 10.22541) every cycle.
    # Always page with a deterministic sort. Crossref's default (relevance)
    # ordering is unstable under deep cursor paging and silently truncates the
    # walk (observed: a member+type backfill stopping at ~150 records); sorting
    # by `indexed` makes both full backfills and incremental runs walk reliably
    # and surfaces newest-changed records first (so `max_records` stays useful).
    if sort is None:
        sort, order = "indexed", "desc"

    # `full` forces a complete backfill (ignore prior events); an explicit
    # `since` overrides the derived window. Otherwise derive it from the last
    # completed harvest event for this source.
    if full:
        since = None
    elif since is None:
        # max_records__isnull=True — a capped run (e.g. a 20-record smoke test
        # or a manual "Harvest now" with a limit) must never become the
        # incremental watermark. Records that existed upstream before that
        # run's completed_at but were excluded purely by the cap were never
        # actually pulled; if we advanced "since" to that run's timestamp
        # anyway, every later incremental run would permanently skip them
        # (they're older than the window and Crossref has no reason to
        # re-index them again). Only a full/unbounded run has actually walked
        # everything up to its completion time, so only that can safely
        # advance the watermark.
        last_completed = (
            HarvestingEvent.objects.filter(
                source=source, status="completed", completed_at__isnull=False, max_records__isnull=True
            )
            .exclude(id=event.id)
            .order_by("-completed_at")
            .first()
        )
        if last_completed:
            since = (last_completed.completed_at.date() - timedelta(days=2)).isoformat()

    try:
        logger.info(
            "Starting Crossref harvest: prefix=%s filter=%s titles=%s issn=%s doi_contains=%s since=%s max_records=%s",
            resolved_prefix,
            extra_filters,
            source_titles,
            issn,
            doi_contains,
            since,
            max_records,
        )
        stats = HarvestStats()
        # A full backfill of a shared-member source (e.g. ESS Open Archive's
        # ~94k member:311 posted-content slice) is too large for one reliable
        # cursor walk over hours. Partition it into yearly deposit-date windows
        # so each walk is bounded and a transient failure costs only one window.
        # Incremental runs (since set), prefix-scoped sources, and bounded
        # `max_records` smoke tests keep a single walk.
        windowed = bool(raw_filter) and since is None and not max_records
        windows = _created_date_windows() if windowed else [(None, None)]

        saved = 0
        seen = 0
        remaining = max_records
        for created_from, created_until in windows:
            if remaining is not None and remaining <= 0:
                break
            if windowed:
                logger.info("Crossref backfill window: created %s..%s", created_from, created_until)
            window_saved, window_seen = parse_crossref_response_and_save_works(
                source,
                event,
                prefix=resolved_prefix,
                source_titles=source_titles,
                extra_filters=extra_filters,
                fetch_abstract_from_publisher=fetch_abstract_from_publisher,
                max_records=remaining,
                warning_collector=warning_collector,
                update_existing=update_existing,
                stats=stats,
                sort=sort,
                order=order,
                since=since,
                doi_contains=doi_contains,
                created_from=created_from,
                created_until=created_until,
                issn=issn,
            )
            saved += window_saved
            seen += window_seen
            if remaining is not None:
                remaining -= window_seen

        spatial_count, temporal_count = complete_harvest(event, stats, warning_collector)

        subject, body = render_harvest_email(
            "email/harvest_success.en.txt",
            {
                "subject_prefix": "Crossref ",
                "source_label": source.name,
                "detail_header": "Crossref harvest details:",
                "source_name": source.name,
                "source_url": None,
                "url_label": None,
                "collection_label": None,
                "records_added_label": "New works saved",
                "records_added": stats.created,
                "records_updated_label": "Updated works",
                "records_updated": stats.updated,
                "spatial_label": "Articles with spatial metadata",
                "spatial_count": spatial_count,
                "temporal_label": "Articles with temporal metadata",
                "temporal_count": temporal_count,
                "event_started": f"{event.started_at:%Y-%m-%d %H:%M:%S}",
                "event_completed": f"{event.completed_at:%Y-%m-%d %H:%M:%S}",
                "warning_summary": warning_collector.get_summary(),
                "resolved_prefix": filter_label,
                "container_title_filters": ", ".join(source_titles) if source_titles else "<all>",
                "openalex_source_id": None,
                "records_seen": seen,
                "records_processed": None,
            },
            event=event,
        )
        send_harvest_email(user, subject, body)

    except Exception as e:
        logger.error(
            "Crossref harvesting failed for source %s: %s",
            source.url_field,
            str(e),
        )
        fail_harvest(event, e, warning_collector)
        subject, body = render_harvest_email(
            "email/harvest_failure.en.txt",
            {
                "subject_prefix": "Crossref ",
                "source_label": source.name,
                "source_type_label": "Crossref",
                "source_name": source.name,
                "source_url": None,
                "collection_label": None,
                "resolved_prefix": filter_label,
                "event_started": None,
                "event_failed": None,
                "error": str(e),
                "warning_summary": "",
            },
        )
        send_harvest_email(user, subject, body, fail_silently=True)
        raise
    finally:
        logger.removeHandler(warning_collector)


def harvest_crossref_book_list(
    source_id,
    user=None,
    max_records=None,
    book_isbns=None,
    update_existing=False,
    event_id=None,
    trigger_source=None,
):
    """Harvest all chapters from a list of book ISBNs via Crossref.

    Designed for proceedings published as separate books per year (e.g. AGILE
    Springer LNCS), where each book has its own ISBN and all chapters share
    the same DOI prefix. Calls ``parse_crossref_response_and_save_works`` once
    per ISBN with ``filter=isbn:{isbn}`` appended, accumulating results into a
    single ``HarvestingEvent``.

    ``book_isbns`` overrides the per-call list; when omitted the caller is
    expected to pass the list via the management command or schedule kwargs.
    The prefix is read from ``source.doi_prefix``.
    """
    user = resolve_user(user)
    source = Source.objects.get(id=source_id)
    event = start_harvesting_event(source, event_id, trigger_source, max_records)
    if event is None:
        return

    warning_collector = HarvestWarningCollector()
    warning_collector.setLevel(logging.INFO)
    logger.addHandler(warning_collector)

    resolved_prefix = (source.doi_prefix if source else None) or "10.1007"
    isbns = book_isbns or []

    try:
        logger.info(
            "Starting Crossref book-list harvest: prefix=%s, %d ISBN(s)",
            resolved_prefix,
            len(isbns),
        )
        stats = HarvestStats()
        total_seen = 0

        for isbn in isbns:
            # Crossref isbn filter requires the plain 13-digit form — hyphens
            # cause zero results even though the ISBN is valid.
            isbn_plain = isbn.replace("-", "")
            logger.info("Harvesting ISBN %s", isbn)
            event.append_log(f"Harvesting ISBN {isbn}")
            _, seen = parse_crossref_response_and_save_works(
                source,
                event,
                prefix=resolved_prefix,
                extra_filters=[f"isbn:{isbn_plain}"],
                fetch_abstract_from_publisher=True,
                max_records=max_records,
                warning_collector=warning_collector,
                update_existing=update_existing,
                stats=stats,
                harvester_name="harvest_crossref_book_list",
            )
            total_seen += seen
            if max_records and total_seen >= max_records:
                break

        spatial_count, temporal_count = complete_harvest(event, stats, warning_collector)

        subject, body = render_harvest_email(
            "email/harvest_success.en.txt",
            {
                "subject_prefix": "Crossref (book list) ",
                "source_label": source.name,
                "detail_header": "Crossref book-list harvest details:",
                "source_name": source.name,
                "source_url": None,
                "url_label": None,
                "collection_label": None,
                "records_added_label": "New works saved",
                "records_added": stats.created,
                "records_updated_label": "Updated works",
                "records_updated": stats.updated,
                "spatial_label": "Chapters with spatial metadata",
                "spatial_count": spatial_count,
                "temporal_label": "Chapters with temporal metadata",
                "temporal_count": temporal_count,
                "event_started": f"{event.started_at:%Y-%m-%d %H:%M:%S}",
                "event_completed": f"{event.completed_at:%Y-%m-%d %H:%M:%S}",
                "warning_summary": warning_collector.get_summary(),
                "resolved_prefix": resolved_prefix,
                "container_title_filters": f"{len(isbns)} ISBN(s)",
                "openalex_source_id": None,
                "records_seen": total_seen,
                "records_processed": None,
            },
            event=event,
        )
        send_harvest_email(user, subject, body)

    except Exception as e:
        logger.error("Crossref book-list harvest failed for source %s: %s", source.name, str(e))
        fail_harvest(event, e, warning_collector)
        subject, body = render_harvest_email(
            "email/harvest_failure.en.txt",
            {
                "subject_prefix": "Crossref (book list) ",
                "source_label": source.name,
                "source_type_label": "Crossref",
                "source_name": source.name,
                "source_url": None,
                "collection_label": None,
                "resolved_prefix": resolved_prefix,
                "event_started": None,
                "event_failed": None,
                "error": str(e),
                "warning_summary": "",
            },
        )
        send_harvest_email(user, subject, body, fail_silently=True)
        raise
    finally:
        logger.removeHandler(warning_collector)


def get_user_contributions_source():
    """Fetch-or-create the dedicated Source that owns user-submitted DOIs.

    Idempotent: returns the existing "User contributions" Source if present,
    otherwise creates it (manual-only, crossref-prefix type) together with its
    auto-collection. Resilient on fresh databases where no migration-seeded row
    exists yet.
    """
    source, created = Source.objects.get_or_create(
        name=USER_CONTRIBUTIONS_SOURCE_NAME,
        defaults={
            "url_field": user_contributions_source_url(),
            "source_type": "crossref-prefix",
            "harvest_interval_minutes": 0,
        },
    )
    if created:
        logger.info("Created the '%s' source (id=%s).", USER_CONTRIBUTIONS_SOURCE_NAME, source.id)
    if source.collection_id is None:
        ensure_collection_for_source(source)
        source.save(update_fields=["collection"])
    return source


def _fetch_crossref_item(doi, session):
    """Fetch the single Crossref ``works`` record for ``doi``.

    Returns the ``message`` dict, or ``None`` on a 404, network/JSON error, or
    empty payload (callers map ``None`` to their own "not found" result).
    """
    try:
        resp = session.get(f"{CROSSREF_API_URL}/{doi}", timeout=CROSSREF_HTTP_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return (resp.json() or {}).get("message") or None
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Crossref single-DOI lookup failed for %s: %s", doi, exc)
        return None


def harvest_crossref_doi(doi, user=None, source_id=None, trigger_source="api"):
    """Harvest a single DOI from Crossref into a ``Work`` (synchronous).

    Used by the /contribute/ "add a work by DOI" flow. Fetches the one Crossref
    record, runs inline OpenAlex enrichment (via ``_crossref_item_to_work_kwargs``)
    and then OpenAIRE enrichment synchronously, and attaches the work to the
    dedicated "User contributions" Source/collection.

    Deliberately does *not* call ``complete_harvest`` — that would enqueue the
    async OpenAIRE sweep and send harvest-completion emails, neither of which
    fits an interactive single-DOI contribution.

    Returns ``(work_or_none, action)`` where ``action`` is one of ``"created"``,
    ``"exists"`` (a Work with this DOI already existed) or ``"not_found"``
    (Crossref has no record for the DOI).
    """
    user = resolve_user(user)
    source = Source.objects.get(id=source_id) if source_id else get_user_contributions_source()

    session = _crossref_session()
    try:
        item = _fetch_crossref_item(doi, session)
        if not item:
            return None, "not_found"

        event = HarvestingEvent.objects.create(source=source, status="in_progress", trigger_source=trigger_source)
        kwargs = _crossref_item_to_work_kwargs(
            item,
            source,
            event,
            fetch_abstract_from_publisher=True,
            abstract_session=session,
            harvester_name="harvest_crossref_doi",
        )
        if not kwargs:
            event.status = "failed"
            event.save(update_fields=["status"])
            return None, "not_found"

        work, action = _save_or_update_work(kwargs, source, event, update_existing=False)
        if action != "created":
            # DOI already known — surface the existing work to the caller.
            event.status = "completed"
            event.completed_at = timezone.now()
            event.save(update_fields=["status", "completed_at"])
            return work, "exists"

        link_openalex_authors(work, kwargs)
        if source.collection_id:
            work.collections.add(source.collection_id)
        _try_bok_pdf_extraction(work, kwargs.get("doi", ""), session)

        # OpenAIRE enrichment, synchronous (fill-if-empty; never raises).
        try:
            enrich_work_from_openaire(work)
        except Exception as exc:  # noqa: BLE001 — enrichment must never fail a contribution
            logger.info("OpenAIRE enrichment failed for %s: %s", doi, exc)

        event.status = "completed"
        event.completed_at = timezone.now()
        event.save(update_fields=["status", "completed_at"])
        return work, "created"
    finally:
        session.close()


def _refresh_source_geometry_temporal(work, session):
    """Re-extract geometry + temporal extent from the work's landing page and
    override the stored values **unless a user contributed them**.

    Spatial / temporal metadata is not part of the Crossref JSON — it is parsed
    from the publisher landing-page HTML (DC.SpatialCoverage / geo+json /
    JSON-LD), the same source the OAI/prefix harvesters use. The re-harvest
    refreshes it from source, but a curator/contributor's geometry or temporal
    extent (recorded as a ``contribution`` event in provenance) is never
    clobbered — see :func:`works.utils.provenance.work_has_contribution_kind`.

    Returns ``{"geometry": <status>, "temporal": <status>}`` where each status
    is one of ``"updated"``, ``"preserved_user_contribution"``,
    ``"no_source_value"``, or ``"skipped"`` (no landing page / fetch failed).
    """
    info = {"geometry": "skipped", "temporal": "skipped"}

    spatial_locked = work_has_contribution_kind(work, "spatial")
    temporal_locked = work_has_contribution_kind(work, "temporal")
    if spatial_locked:
        info["geometry"] = "preserved_user_contribution"
    if temporal_locked:
        info["temporal"] = "preserved_user_contribution"
    if spatial_locked and temporal_locked:
        return info
    if not work.url:
        return info

    try:
        # Force an HTML Accept header: the Crossref session defaults to
        # `Accept: application/json`, which makes a doi.org URL resolve via DOI
        # *content negotiation* to Crossref's metadata transform endpoint
        # instead of the publisher's HTML landing page — so we'd never see the
        # spatial/temporal markup. Override it to land on the real article page.
        resp = fetch_with_deadline(
            lambda: session.get(
                work.url,
                timeout=CROSSREF_HTTP_TIMEOUT,
                allow_redirects=True,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            ),
            _LANDING_PAGE_FETCH_DEADLINE,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
    except (requests.RequestException, ValueError, TimeoutError) as exc:
        logger.info("Re-harvest landing-page fetch failed for %s: %s", work.url, exc)
        return info

    prov = work.provenance if isinstance(work.provenance, dict) else {}
    metadata_sources = prov.setdefault("metadata_sources", {})
    changed_fields = []

    if not spatial_locked:
        try:
            geom, geom_label = extract_geometry_from_html(soup, base_url=work.url)
        except Exception as exc:  # noqa: BLE001 — extraction must never fail a re-harvest
            geom, geom_label = None, None
            logger.info("Re-harvest geometry extraction failed for %s: %s", work.url, exc)
        if geom is not None and not _is_empty_for_update(geom):
            work.geometry = geom
            metadata_sources["geometry"] = geom_label or "reharvest_html"
            changed_fields.append("geometry")
            info["geometry"] = "updated"
        else:
            info["geometry"] = "no_source_value"

    if not temporal_locked:
        try:
            ts, te = extract_timeperiod_from_html(soup)
        except Exception as exc:  # noqa: BLE001 — extraction must never fail a re-harvest
            ts, te = [], []
            logger.info("Re-harvest temporal extraction failed for %s: %s", work.url, exc)
        # extract_timeperiod_from_html returns [None]/[None] (not []) when the
        # page has no dates — guard on a real (non-None) value, not truthiness.
        has_temporal = any(d is not None for d in (ts or [])) or any(d is not None for d in (te or []))
        if has_temporal:
            work.timeperiod_startdate = ts or []
            work.timeperiod_enddate = te or []
            metadata_sources["timeperiod"] = "reharvest_html"
            changed_fields += ["timeperiod_startdate", "timeperiod_enddate"]
            info["temporal"] = "updated"
        else:
            info["temporal"] = "no_source_value"

    if changed_fields:
        append_event(
            work,
            "reharvest_source_extents",
            geometry="updated" if info["geometry"] == "updated" else None,
            temporal="updated" if info["temporal"] == "updated" else None,
        )
        work.provenance = prov
        # Bump lastUpdate so the landing-page cache invalidates and the map
        # reflects the new geometry immediately.
        work.save(update_fields=changed_fields + ["provenance", "lastUpdate"])

    return info


def reharvest_work(work, user=None, trigger_source="manual"):
    """Re-harvest a single existing ``Work`` by its DOI (synchronous).

    Used by the admin "Re-harvest" button on the work landing page. Re-fetches
    the Crossref record for ``work.doi``, re-runs inline OpenAlex enrichment
    (via ``_crossref_item_to_work_kwargs``) and then OpenAIRE enrichment, and
    updates the work **in place** using the careful-update policy
    (``_carefully_update_work``) — refreshing upstream metadata while preserving
    user/curator contributions (status, geometry, temporal extent, and
    abstract/keywords/authors when the fresh harvest brings nothing).

    Unlike ``harvest_crossref_doi`` this does not go through
    ``_save_or_update_work``: that helper's per-source dedup would treat the
    Crossref fetch as a *different* source from the work's original origin and
    refuse to update. Re-harvest deliberately bypasses the source-match guard
    and updates the existing record regardless of its original source.

    Geometry and temporal extent — which Crossref does not carry — are
    re-extracted from the landing page and **overridden in place unless a user
    contributed them** (see :func:`_refresh_source_geometry_temporal`).

    Returns ``(work_or_none, action, info)`` where ``action`` is one of:
      * ``"updated"`` — metadata refreshed in place,
      * ``"no_doi"`` — the work has no DOI to re-harvest from,
      * ``"not_found"`` — Crossref has no (usable) record for the DOI.
    ``info`` is the geometry/temporal outcome dict from
    :func:`_refresh_source_geometry_temporal` (empty on the non-``"updated"`` paths).
    """
    if not work.doi:
        return None, "no_doi", {}

    user = resolve_user(user)
    source = work.source or get_user_contributions_source()

    session = _crossref_session()
    try:
        item = _fetch_crossref_item(work.doi, session)
        if not item:
            return work, "not_found", {}

        event = HarvestingEvent.objects.create(
            source=source, user=user, status="in_progress", trigger_source=trigger_source
        )
        try:
            kwargs = _crossref_item_to_work_kwargs(
                item,
                source,
                event,
                fetch_abstract_from_publisher=True,
                abstract_session=session,
                harvester_name="reharvest_work",
            )
            if not kwargs:
                event.status = "failed"
                event.save(update_fields=["status"])
                return work, "not_found", {}

            # Careful-update preserves user/curator data and appends a
            # `harvest_update` provenance event; OpenAlex enrichment already ran
            # inside _crossref_item_to_work_kwargs.
            _carefully_update_work(work, kwargs, event)
            extents_info = _refresh_source_geometry_temporal(work, session)
            _try_bok_pdf_extraction(work, work.doi, session)

            # OpenAIRE enrichment, synchronous (fill-if-empty; never raises).
            try:
                enrich_work_from_openaire(work)
            except Exception as exc:  # noqa: BLE001 — enrichment must never fail a re-harvest
                logger.info("OpenAIRE enrichment failed for %s: %s", work.doi, exc)

            event.status = "completed"
            event.completed_at = timezone.now()
            event.save(update_fields=["status", "completed_at"])
            logger.info("Re-harvested work id=%s (DOI %s) by user %s", work.id, work.doi, getattr(user, "id", None))
            return work, "updated", extents_info
        except Exception:
            event.status = "failed"
            event.save(update_fields=["status"])
            raise
    finally:
        session.close()
