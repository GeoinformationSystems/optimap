# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""OpenAIRE enrichment — a second metadata source besides OpenAlex.

OpenAIRE's Graph API carries abstracts (and other metadata) for works whose
harvest origin did not supply them — notably the AGILE Springer LNCS chapters
(DOI prefix ``10.1007/978-…``) for which Crossref has no abstract and the
publisher landing page is not scraped.

A single DOI is resolved via ``GET <OPENAIRE_API_URL>?pid=<doi>`` which returns
``{"header": {...}, "results": [ {...} ]}``; the abstract lives in
``results[0].descriptions[]``.

This module exposes:
- ``fetch_openaire_record`` / ``build_openaire_fields`` — pure lookup + extraction.
- ``enrich_work_from_openaire`` — the single per-work enricher, reused by both the
  live post-harvest sweep and the ``enrich_openaire`` backfill command. It applies
  the fill-if-empty policy (``works.harvesting.enrichment.apply_enrichment``) and
  records every decision in ``Work.provenance`` (``metadata_sources``, an
  ``openaire_enrich`` event, and an ``openaire_match`` block).
- ``enrich_event_from_openaire`` — the async sweep enqueued by ``complete_harvest``.

Both ``enrich_*`` callables are re-exported from ``works.tasks`` so Django-Q
dotted-path schedules resolve.
"""

import html
import logging
import re
import time

import requests
from django.conf import settings
from django.core.mail import send_mail

from works.harvesting.enrichment import apply_enrichment
from works.harvesting.sessions import (
    OPENAIRE_API_URL,
    OPENAIRE_HTTP_TIMEOUT,
    OPENAIRE_TOKEN_EXCHANGE_URL,
    OPENAIRE_USER_AGENT,
    _openaire_session,
)
from works.models import HarvestingEvent, Work
from works.utils.orcid import normalize_orcid
from works.utils.provenance import append_event

logger = logging.getLogger(__name__)


class OpenAireUnavailable(RuntimeError):
    """OpenAIRE returned a *systemic* error, not a clean "not found".

    Distinguishes "OpenAIRE is refusing or unable to answer us" — an expired /
    revoked token, an IP block, rate-limiting, or a server/connection failure —
    from "this DOI is simply not in OpenAIRE" (a normal per-work outcome, still
    returned as ``None``). Batch callers (the post-harvest sweep and the
    backfill) opt in via ``raise_on_unavailable=True`` and use this to trip a
    circuit breaker instead of grinding through thousands of doomed requests.
    Carries the HTTP status (``None`` for connection/timeout errors).
    """

    def __init__(self, message, *, status=None, doi=None):
        super().__init__(message)
        self.status = status
        self.doi = doi


# HTTP statuses that mean "OpenAIRE refused / cannot serve us" (plus any 5xx),
# as opposed to 404 / 200-with-no-results which mean "not found".
_OPENAIRE_SYSTEMIC_STATUSES = frozenset({401, 403, 408, 429})


class _SystemicFailureBreaker:
    """Counts *consecutive* systemic failures; trips at ``threshold``.

    A single transient blip should not abort a multi-hour batch, but a run of
    identical failures (e.g. every request returning 403) means OpenAIRE access
    is broken and there is no point continuing.
    """

    def __init__(self, threshold):
        self.threshold = max(1, int(threshold))
        self.consecutive = 0
        self.last_error = None

    def ok(self):
        self.consecutive = 0

    def systemic(self, exc):
        """Record a systemic failure; return True once the threshold is reached."""
        self.consecutive += 1
        self.last_error = exc
        return self.consecutive >= self.threshold


def notify_openaire_abort(context, breaker, *, processed, total, trigger_source="scheduled"):
    """Email active staff (and log ERROR) that an OpenAIRE batch aborted.

    Turns a silently failing, hours-long "dead" job into a single actionable
    alert. Mirrors the staff-email pattern used by ``check_service_token_renewals``.
    """
    from django.contrib.auth import get_user_model

    from works.models import EmailLog
    from works.utils.email import render_email

    last_error = breaker.last_error
    status = getattr(last_error, "status", None)
    logger.error(
        "OpenAIRE %s aborted after %d consecutive systemic failures (HTTP %s): %s",
        context,
        breaker.consecutive,
        status if status is not None else "connection error",
        last_error,
    )

    User = get_user_model()
    recipients = list(
        User.objects.filter(is_staff=True, is_active=True).exclude(email="").values_list("email", flat=True)
    )
    if not recipients:
        logger.warning("notify_openaire_abort: no staff emails configured; alert not sent.")
        return

    subject, body = render_email(
        "email/openaire_enrich_aborted.en.txt",
        {
            "context": context,
            "consecutive": breaker.consecutive,
            "status": status if status is not None else "connection error",
            "error": str(last_error),
            "processed": processed,
            "total": total,
            "base_url": settings.BASE_URL,
        },
    )
    for recipient in recipients:
        try:
            send_mail(subject, body, settings.EMAIL_HOST_USER, [recipient], fail_silently=False)
            EmailLog.log_email(recipient, subject, body, trigger_source=trigger_source, status="success")
        except Exception as ex:  # noqa: BLE001
            logger.exception("Failed to send OpenAIRE abort alert to %s.", recipient)
            EmailLog.log_email(
                recipient, subject, body, trigger_source=trigger_source, status="failed", error_message=str(ex)
            )


def openaire_task_q_options():
    """Per-task Django-Q options for the throttled OpenAIRE enrichment tasks.

    The sweep / backfill sleep ``OPTIMAP_OPENAIRE_ENRICH_THROTTLE`` seconds
    between requests, so a run over many DOIs can take hours — far longer than
    the global ``Q_CLUSTER['timeout']`` (600s). Override the per-task ``timeout``
    and set ``retry`` above it so Django-Q does not re-queue the still-running
    task. Returns ``{}`` when the override is disabled (set to 0), keeping the
    cluster default. Pass the result as ``q_options=`` to ``async_task``.
    """
    timeout = getattr(settings, "OPTIMAP_OPENAIRE_ENRICH_TASK_TIMEOUT", 0)
    if not timeout:
        return {}
    return {"timeout": int(timeout), "retry": int(timeout) + 60 * 10}


# OpenAIRE attaches automated classifications under these subject schemes; they
# are not free-text keywords and would pollute Work.keywords, so we drop them.
_AUTOMATED_SUBJECT_SCHEMES = {"sdg", "fos"}

# Work fields OpenAIRE may fill (fill-if-empty). `type` is excluded (OpenAIRE's
# coarse "publication"/"dataset" is less useful than the source default).
ENRICHABLE_FIELDS = (
    "abstract",
    "keywords",
    "authors",
    "volume",
    "issue",
    "first_page",
    "last_page",
    "language",
    "publisher",
)

# OpenAIRE `container` sub-keys → Work journal-citation fields.
_CONTAINER_FIELD_MAP = {"vol": "volume", "iss": "issue", "sp": "first_page", "ep": "last_page"}

# Public OpenAIRE Explore landing page for a matched research product, keyed by
# the OpenAIRE internal id (e.g. ``doi_dedup___::…``). Recorded in
# ``provenance.openaire_match.url`` so the work landing page can offer a
# "View in OpenAIRE" link, mirroring the OpenAlex link.
OPENAIRE_EXPLORE_RESULT_URL = "https://explore.openaire.eu/search/result?id="

_MARKUP_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_markup(text):
    """Strip XML/HTML tags and unescape entities, returning plain text.

    OpenAIRE abstracts arrive as JATS fragments (``<jats:p>…</jats:p>``,
    ``<jats:italic>``, …) that must not leak into ``Work.abstract``. Tags are
    removed, HTML entities unescaped, and runs of whitespace collapsed.
    """
    if not text or not isinstance(text, str):
        return text
    text = _MARKUP_RE.sub("", text)
    text = html.unescape(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def get_openaire_access_token():
    """Return a valid OpenAIRE access token, or ``None``.

    Backed by the refresh token stored in the ``ServiceToken`` table (rotated by
    staff in the Django admin). Returns the cached access token while it is still
    valid; otherwise exchanges the refresh token for a fresh one via
    ``GET OPENAIRE_TOKEN_EXCHANGE_URL?refreshToken=...`` (the access token is
    valid ~1h) and caches it on the row. Returns ``None`` when no refresh token
    is configured or the exchange fails — callers then fall back to the static
    ``OPTIMAP_OPENAIRE_TOKEN`` or anonymous access.

    Uses a plain ``requests.get`` (not ``_openaire_session``) to avoid recursing
    back into bearer-token resolution.
    """
    from works.models import ServiceToken

    token_row = ServiceToken.objects.filter(service=ServiceToken.OPENAIRE).first()
    if token_row is None or not token_row.refresh_token:
        return None
    if token_row.access_token_valid():
        return token_row.access_token

    try:
        resp = requests.get(
            OPENAIRE_TOKEN_EXCHANGE_URL,
            params={"refreshToken": token_row.refresh_token},
            headers={"User-Agent": OPENAIRE_USER_AGENT, "Accept": "application/json"},
            timeout=OPENAIRE_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("OpenAIRE token exchange failed: %s", exc)
        return None

    access_token = data.get("access_token") if isinstance(data, dict) else None
    if not access_token:
        logger.warning("OpenAIRE token exchange returned no access_token")
        return None

    ttl = data.get("expires_in") if isinstance(data, dict) else None
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        ttl = settings.OPTIMAP_OPENAIRE_ACCESS_TOKEN_TTL
    token_row.store_access_token(access_token, ttl)
    logger.info("Exchanged OpenAIRE refresh token for a new access token (ttl=%ss).", ttl)
    return access_token


def fetch_openaire_record(doi, session=None, *, raise_on_unavailable=False):
    """Look up a single research product by DOI. Returns the record dict or None.

    By default every error is swallowed (logged, returns ``None``) so a single
    live enrichment never aborts a harvest. Batch callers pass
    ``raise_on_unavailable=True`` to get an :class:`OpenAireUnavailable` for
    *systemic* failures (HTTP 401/403/408/429, any 5xx, or a connection/timeout
    error) so they can trip a circuit breaker; a genuine 404 or an empty result
    set is still returned as ``None`` (a normal "not in OpenAIRE" outcome).
    """
    if not doi:
        return None
    owns_session = session is None
    session = session or _openaire_session()
    try:
        resp = session.get(OPENAIRE_API_URL, params={"pid": doi}, timeout=OPENAIRE_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if raise_on_unavailable and status is not None and (status in _OPENAIRE_SYSTEMIC_STATUSES or status >= 500):
            raise OpenAireUnavailable(str(exc), status=status, doi=doi) from exc
        logger.warning("OpenAIRE lookup failed for DOI %s: %s", doi, exc)
        return None
    except requests.RequestException as exc:
        # Connection reset / timeout / DNS — systemic, not a clean "not found".
        if raise_on_unavailable:
            raise OpenAireUnavailable(str(exc), status=None, doi=doi) from exc
        logger.warning("OpenAIRE lookup failed for DOI %s: %s", doi, exc)
        return None
    except ValueError as exc:
        logger.warning("OpenAIRE lookup failed for DOI %s: %s", doi, exc)
        return None
    finally:
        if owns_session:
            session.close()

    results = data.get("results") or []
    if not results:
        return None
    return results[0]


def _best_description(descriptions):
    """Return the longest non-empty description (the fullest abstract).

    Markup is stripped first (OpenAIRE wraps abstracts in JATS tags), so the
    "longest" comparison and the stored value are both on plain text.
    """
    if not descriptions:
        return None
    candidates = [c for c in (_strip_markup(d) for d in descriptions if isinstance(d, str)) if c]
    if not candidates:
        return None
    return max(candidates, key=len)


def _keywords_from_subjects(subjects):
    """Return free-text keyword values, dropping automated classification schemes."""
    if not subjects:
        return []
    keywords = []
    for entry in subjects:
        subject = (entry or {}).get("subject") or {}
        scheme = (subject.get("scheme") or "").strip().lower()
        value = (subject.get("value") or "").strip()
        if not value or scheme in _AUTOMATED_SUBJECT_SCHEMES:
            continue
        if value not in keywords:
            keywords.append(value)
    return keywords


def build_openaire_fields(record):
    """Extract enrichment candidates from an OpenAIRE record (pure, no DB).

    Returns ``{field: value}`` for the subset of ``ENRICHABLE_FIELDS`` that the
    record provides; empty values are omitted so ``apply_enrichment`` skips them.
    """
    if not record:
        return {}

    candidates = {}

    abstract = _best_description(record.get("descriptions"))
    if abstract:
        candidates["abstract"] = abstract

    keywords = _keywords_from_subjects(record.get("subjects"))
    if keywords:
        candidates["keywords"] = keywords

    authors = [a["fullName"].strip() for a in (record.get("authors") or []) if (a or {}).get("fullName")]
    if authors:
        candidates["authors"] = authors

    container = record.get("container") or {}
    for source_key, field in _CONTAINER_FIELD_MAP.items():
        value = container.get(source_key)
        value = str(value).strip() if value is not None else ""
        if value:
            candidates[field] = value

    language = ((record.get("language") or {}).get("code") or "").strip()
    if language:
        candidates["language"] = language

    publisher = (record.get("publisher") or "").strip()
    if publisher:
        candidates["publisher"] = publisher

    # OpenAIRE values are free-text; cap each CharField-backed candidate at its
    # model max_length so an over-long value can't raise DataError on save.
    # (abstract is a TextField; keywords/authors are list fields — all unbounded.)
    for field in ("volume", "issue", "first_page", "last_page", "language", "publisher"):
        value = candidates.get(field)
        if value:
            candidates[field] = value[: Work._meta.get_field(field).max_length]

    return candidates


def _author_records_from_openaire(record) -> list:
    """Extract structured author records with ORCID from an OpenAIRE result record.

    OpenAIRE author objects may include a ``pid`` list with scheme/value pairs, e.g.
    ``[{"scheme": "orcid", "value": "0000-0002-1825-0097"}]``. Returns records
    suitable for :func:`works.services.authors.link_work_authors`, skipping authors
    with no identifier.
    """
    records = []
    for i, author in enumerate(record.get("authors") or []):
        if not isinstance(author, dict):
            continue
        name = (author.get("fullName") or "").strip()
        orcid = None
        for pid in author.get("pid") or []:
            if isinstance(pid, dict) and (pid.get("scheme") or "").lower() == "orcid":
                orcid = normalize_orcid(pid.get("value"))
                if orcid:
                    break
        if orcid:
            records.append({"name": name, "orcid": orcid, "openalex_id": None, "rank": i})
    return records


def enrich_work_from_openaire(work, *, session=None, save=True, raise_on_unavailable=False):
    """Enrich a single Work from OpenAIRE (fill-if-empty). Returns True if changed.

    Records the outcome in ``work.provenance`` regardless of whether anything was
    filled: an ``openaire_match`` block (``status`` matched/none) plus, on a match,
    an ``openaire_enrich`` event listing ``fields_filled`` and
    ``fields_offered_not_applied`` (the conflicts resolved in favour of the
    existing value).

    ``raise_on_unavailable`` is forwarded to :func:`fetch_openaire_record`: batch
    callers set it so a systemic OpenAIRE failure raises :class:`OpenAireUnavailable`
    (to trip their circuit breaker) instead of being recorded as ``status: none``.
    """
    doi = (work.doi or "").strip()
    record = fetch_openaire_record(doi, session=session, raise_on_unavailable=raise_on_unavailable) if doi else None

    if not record:
        provenance = work.provenance if isinstance(work.provenance, dict) else {}
        provenance["openaire_match"] = {"status": "none", "num_found": 0}
        work.provenance = provenance
        if save:
            work.save(update_fields=["provenance", "lastUpdate"])
        return False

    openaire_id = record.get("id")
    candidates = build_openaire_fields(record)
    filled, offered = apply_enrichment(work, candidates, "openaire")

    # Best-effort: link author ORCIDs extracted from OpenAIRE pid fields.
    try:
        author_records = _author_records_from_openaire(record)
        if author_records:
            from works.services.authors import link_work_authors

            link_work_authors(work, author_records, source="openaire")
    except Exception as exc:
        logger.debug("OpenAIRE author ORCID linking failed for work %s: %s", work.id, exc)

    provenance = work.provenance if isinstance(work.provenance, dict) else {}
    provenance["openaire_match"] = {
        "status": "matched",
        "openaire_id": openaire_id,
        "num_found": 1,
        "url": f"{OPENAIRE_EXPLORE_RESULT_URL}{openaire_id}" if openaire_id else None,
    }
    work.provenance = provenance
    append_event(
        work,
        "openaire_enrich",
        openaire_id=openaire_id,
        doi=doi or None,
        source_url=f"{OPENAIRE_API_URL}?pid={doi}",
        fields_filled=filled or None,
        fields_offered_not_applied=offered or None,
    )

    if save:
        work.save(update_fields=[*filled, "provenance", "lastUpdate"])
    return bool(filled)


def enrich_event_from_openaire(event_id, throttle=None, after_id=0, carry=None):
    """Async sweep: check a harvest event's DOI-bearing works against OpenAIRE.

    Note: this function's dotted path is listed in
    ``works.signals._SELF_NOTIFYING_TASK_FUNCS`` — it already emails staff via
    ``notify_openaire_abort`` before re-raising on abort, so the generic
    ``post_execute`` failure notifier skips it. Keep that list in sync if this
    function is ever renamed.

    Enqueued by ``complete_harvest`` after every successful harvest (gated by
    ``OPTIMAP_OPENAIRE_ENRICH_ON_HARVEST``). Every DOI-bearing work that has *not*
    yet been consulted is looked up so the OpenAIRE consultation is recorded in
    ``provenance.openaire_match`` (audit trail) — fields that are empty get filled
    (fill-if-empty), while works that already have everything still get a
    ``matched``/``none`` record (and, on a match, an ``openaire_enrich`` event
    listing the offered-but-not-applied fields). Works that already carry an
    ``openaire_match`` are skipped, so each work is consulted exactly once.
    Throttles between requests to respect OpenAIRE's rate limit (60/hour anonymous,
    7200/hour with a token). Returns the number of works whose fields were filled.

    **Bounded chunks.** Because the throttle can make a full run take hours, the
    sweep processes works only until a wall-clock budget
    (``OPTIMAP_OPENAIRE_ENRICH_CHUNK_SECONDS``) elapses, then re-enqueues itself
    with an ``after_id`` cursor (works are processed in ascending id order). This
    keeps every Django-Q task short, so the cluster's stop signal is honoured
    between chunks and no task outlives the cluster timeout. ``carry`` threads the
    running totals across chunks.

    **Self-resuming across aborts.** The ``after_id`` cursor lives only in the
    continuation task's arguments and is lost when the circuit breaker (below)
    aborts the run. Because consulted works are excluded from the queryset via
    their ``openaire_match`` marker, a fresh re-trigger (default ``after_id=0``)
    still resumes from the first unchecked work rather than re-processing from the
    start, so repeated runs after a systemic outage converge and reach the tail.

    **Circuit breaker.** After ``OPTIMAP_OPENAIRE_ENRICH_ABORT_AFTER`` *consecutive*
    systemic OpenAIRE failures (:class:`OpenAireUnavailable` — auth / rate-limit /
    server / connection), the sweep emails staff and raises so the task is marked
    failed, instead of grinding through thousands of doomed requests.

    Note: the ``enrich_openaire`` backfill command deliberately keeps its
    missing-field filter — this full audit trail is only built going forward, as
    part of the regular post-harvest enrichment.
    """
    try:
        event = HarvestingEvent.objects.get(id=event_id)
    except HarvestingEvent.DoesNotExist:
        logger.warning("OpenAIRE sweep: harvesting event %s not found", event_id)
        return 0

    if throttle is None:
        throttle = settings.OPTIMAP_OPENAIRE_ENRICH_THROTTLE

    # Skip works already consulted in an earlier (possibly aborted) run: every
    # work the sweep touches gets a ``provenance.openaire_match`` block written,
    # matched or not, so its presence is a durable "already checked" marker. This
    # makes the sweep idempotent and self-resuming — a fresh re-trigger after an
    # abort (which discards the in-flight ``after_id`` cursor) naturally continues
    # from the first unchecked work instead of re-spending OpenAIRE's rate budget
    # on works done earlier, so repeated runs converge and reach the tail.
    base_qs = (
        event.works.filter(doi__isnull=False)
        .exclude(doi="")
        .filter(provenance__openaire_match__isnull=True)
        .order_by("id")
    )
    carry = carry or {}
    total = carry.get("total")
    if total is None:
        total = base_qs.count()
        logger.info("OpenAIRE sweep for event %s: %d work(s) with a DOI", event_id, total)
    if not total:
        return 0

    updated = carry.get("updated", 0)
    processed = carry.get("processed", 0)
    deadline = time.monotonic() + settings.OPTIMAP_OPENAIRE_ENRICH_CHUNK_SECONDS
    breaker = _SystemicFailureBreaker(settings.OPTIMAP_OPENAIRE_ENRICH_ABORT_AFTER)
    session = _openaire_session()
    last_id = after_id
    try:
        for work in base_qs.filter(id__gt=after_id).iterator(chunk_size=50):
            try:
                if enrich_work_from_openaire(work, session=session, raise_on_unavailable=True):
                    updated += 1
                breaker.ok()
            except OpenAireUnavailable as exc:
                if breaker.systemic(exc):
                    notify_openaire_abort(
                        f"sweep for harvest event {event_id}", breaker, processed=processed, total=total
                    )
                    raise OpenAireUnavailable(
                        f"OpenAIRE sweep for event {event_id} aborted after "
                        f"{breaker.consecutive} consecutive systemic failures",
                        status=exc.status,
                    ) from exc
                logger.warning("OpenAIRE sweep: systemic failure for work %s (%s): %s", work.id, work.doi, exc)
            except Exception as exc:
                # Non-systemic per-work error: log, do not trip the breaker.
                logger.warning("OpenAIRE enrich failed for work %s (%s): %s", work.id, work.doi, exc)
                breaker.ok()
            last_id = work.id
            processed += 1
            if time.monotonic() >= deadline:
                break
            if throttle:
                time.sleep(throttle)
    finally:
        session.close()

    if base_qs.filter(id__gt=last_id).exists():
        from django_q.tasks import async_task

        async_task(
            "works.harvesting.openaire.enrich_event_from_openaire",
            event_id,
            throttle=throttle,
            after_id=last_id,
            carry={"total": total, "updated": updated, "processed": processed},
            q_options=openaire_task_q_options(),
        )
        logger.info(
            "OpenAIRE sweep for event %s: chunk done (%d/%d processed, id<=%s), continuation enqueued",
            event_id,
            processed,
            total,
            last_id,
        )
        return updated

    logger.info("OpenAIRE sweep for event %s done: %d/%d work(s) updated", event_id, updated, total)
    return updated
