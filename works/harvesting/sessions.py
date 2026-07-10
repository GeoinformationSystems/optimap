# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""HTTP session factories and response sniffers shared by every harvester."""

import hashlib
import logging
import queue
import re
import threading
from urllib.parse import urlparse

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


def fetch_with_deadline(fetch_fn, deadline):
    """Call ``fetch_fn()`` (no args) and give up waiting after ``deadline``
    seconds, raising ``TimeoutError``.

    ``requests``' own ``timeout=`` only bounds the connect and the gap
    *between* individual socket reads — a server that trickles a large HTML
    response slowly enough to keep resetting that per-read timer, without
    ever fully stalling, can make a single call run far longer than intended
    while technically "making progress" the whole time. That already produced
    one confirmed multi-hour hang (a landing-page fetch during an OAI-PMH
    harvest, only killed by Django-Q's own 6000s task-level ceiling).

    Runs ``fetch_fn`` in a background thread and stops waiting after
    ``deadline`` seconds. There is no way to forcibly cancel an in-flight
    ``requests`` call, so a timed-out call is simply abandoned — the
    background thread (``daemon=True``, so it can't block process exit) keeps
    running until the underlying socket read eventually resolves or errors on
    its own; only *this* function's wait is bounded.
    """
    result: queue.Queue = queue.Queue(maxsize=1)

    def _run():
        try:
            result.put(("ok", fetch_fn()))
        except Exception as exc:  # noqa: BLE001 — re-raised in the caller's thread below
            result.put(("error", exc))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(deadline)
    if thread.is_alive():
        raise TimeoutError(f"exceeded {deadline}s wall-clock deadline")
    kind, value = result.get_nowait()
    if kind == "error":
        raise value
    return value


# OAI-PMH ---------------------------------------------------------------------

OAI_HTTP_TIMEOUT = settings.OPTIMAP_OAI_HTTP_TIMEOUT  # seconds; per-request, applies to both connect and read
OAI_RETRY_TOTAL = 3
OAI_USER_AGENT = f"{settings.OPTIMAP_USER_AGENT} oai-pmh"


def _oai_session() -> requests.Session:
    """`requests.Session` configured with retries for transient errors and a
    descriptive User-Agent so upstream operators can identify our traffic.
    Retries cover GET only; 4xx (other than 429) are not retried because they
    almost always indicate a permanent problem (bad URL, removed set)."""
    session = requests.Session()
    retry = Retry(
        total=OAI_RETRY_TOTAL,
        connect=OAI_RETRY_TOTAL,
        read=OAI_RETRY_TOTAL,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": OAI_USER_AGENT,
            "Accept": "text/xml, application/xml, */*",
        }
    )
    return session


def _looks_like_oai_xml(body: bytes) -> bool:
    """Cheap content sniff so we fail fast and clearly when an upstream
    'helpfully' returns an HTML 200 error page instead of an OAI-PMH response."""
    if not body:
        return False
    head = body.lstrip()[:512].lower()
    if head.startswith(b"<?xml"):
        return True
    return b"<oai-pmh" in head


def _short_body(resp: requests.Response, n: int = 240) -> str:
    """Trim a response body for use in error messages."""
    text = resp.text or ""
    text = " ".join(text.split())
    if len(text) > n:
        return text[:n] + "…"
    return text


_POW_MAX_ITERATIONS = 10_000_000


def _try_solve_pow_challenge(session: requests.Session, response: requests.Response) -> bool:
    """Solve a BunkerWeb/HAProxy SHA-256 Proof-of-Work bot-protection challenge.

    Some repositories (e.g. GEO-LEO e-docs) protect their OAI-PMH endpoints
    with a JS PoW challenge. The challenge is fully solvable without a browser:
    find nonce i such that SHA-256(challenge+str(i)) has 0x00 at byte
    challenge_index and 0x41 at challenge_index+1 (where challenge_index is
    int(challenge[0], 16)). On success the server returns a long-lived
    ray_clearance cookie (currently expires 2029-12-31).

    Returns True if the challenge was solved and a clearance cookie is now set.
    """
    if "data-pow=" not in (response.text or ""):
        return False

    m = re.search(r'data-pow="([^"]+)"', response.text)
    if not m:
        return False

    combined = m.group(1)
    parts = combined.split("#")
    if len(parts) != 3:
        return False

    _userkey, challenge, _signature = parts
    challenge_index = int(challenge[0], 16)

    logger.info("Bot-protection PoW challenge detected (challengeIndex=%d); solving…", challenge_index)

    nonce = None
    for i in range(_POW_MAX_ITERATIONS):
        digest = hashlib.sha256((challenge + str(i)).encode()).digest()
        if digest[challenge_index] == 0x00 and digest[challenge_index + 1] == 0x41:
            nonce = i
            break

    if nonce is None:
        logger.warning("PoW challenge: no solution found within %d iterations", _POW_MAX_ITERATIONS)
        return False

    logger.info("PoW solved: nonce=%d", nonce)

    parsed = urlparse(response.url)
    ray_post_url = f"{parsed.scheme}://{parsed.netloc}/_ray"
    ray_response = f"{combined}#{nonce}"

    post_resp = session.post(
        ray_post_url,
        data={"ray_clearance_response": ray_response},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        allow_redirects=False,
    )

    if "ray_clearance" in session.cookies:
        logger.info("Bot protection bypassed (POST status=%d); ray_clearance cookie set", post_resp.status_code)
        return True

    logger.warning("PoW POST returned status=%d but no ray_clearance cookie was set", post_resp.status_code)
    return False


# Crossref --------------------------------------------------------------------

CROSSREF_API_URL = "https://api.crossref.org/works"
# Polite-pool User-Agent — Crossref rate-limits anonymous traffic; the mailto
# in OPTIMAP_USER_AGENT is what puts us in the polite pool.
CROSSREF_USER_AGENT = settings.OPTIMAP_USER_AGENT
CROSSREF_HTTP_TIMEOUT = 60
CROSSREF_PAGE_ROWS = 100


def _crossref_session():
    """Return a requests.Session preconfigured with retries + UA."""
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": CROSSREF_USER_AGENT,
            "Accept": "application/json",
        }
    )
    return session


# DataCite (IGSN samples) -----------------------------------------------------

DATACITE_API_URL = "https://api.datacite.org/dois"
DATACITE_USER_AGENT = settings.OPTIMAP_USER_AGENT
DATACITE_HTTP_TIMEOUT = 60
DATACITE_PAGE_SIZE = 100


def _datacite_session():
    """Return a requests.Session preconfigured with retries + polite UA for DataCite."""
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": DATACITE_USER_AGENT,
            "Accept": "application/json",
        }
    )
    return session


# GFZ IGSN landing pages ------------------------------------------------------

GFZ_LANDING_URL = "https://dataservices.gfz-potsdam.de/igsn/esg/index.php"
GFZ_USER_AGENT = settings.OPTIMAP_USER_AGENT
GFZ_HTTP_TIMEOUT = 30


def _gfz_session():
    """Return a requests.Session for scraping GFZ IGSN landing pages (retries + polite UA)."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": GFZ_USER_AGENT})
    return session


# OpenAlex --------------------------------------------------------------------

OPENALEX_API_URL = "https://api.openalex.org/works"
OPENALEX_USER_AGENT = settings.OPTIMAP_USER_AGENT
OPENALEX_HTTP_TIMEOUT = 60
OPENALEX_PAGE_SIZE = 200  # OpenAlex max per page


def get_openalex_api_key() -> str:
    """Return the OpenAlex API key, preferring the DB ServiceToken over the env var.

    The key can be stored in two places (DB takes precedence):
    - Django admin → Service tokens → OpenAlex API (``refresh_token`` field).
    - ``OPTIMAP_OPENALEX_API_KEY`` environment variable / ``.env`` file.

    The DB lookup is wrapped in a try/except so this is safe to call during
    tests and management commands where the DB may not be fully available.
    """
    try:
        from works.models import ServiceToken

        row = ServiceToken.objects.filter(service=ServiceToken.OPENALEX).first()
        if row and row.refresh_token:
            return row.refresh_token
    except Exception:
        pass
    return getattr(settings, "OPTIMAP_OPENALEX_API_KEY", "") or ""


def _openalex_session():
    """Return a `requests.Session` configured with retries for OpenAlex list queries."""
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": OPENALEX_USER_AGENT,
            "Accept": "application/json",
        }
    )
    api_key = get_openalex_api_key()
    if api_key:
        session.params = {"api_key": api_key}
    return session


# OpenAIRE --------------------------------------------------------------------

# OpenAIRE Graph API v1. A single DOI is looked up via `?pid=<doi>`; the abstract
# lives in results[0].descriptions[].
OPENAIRE_API_URL = "https://api.openaire.eu/graph/v1/researchProducts"
OPENAIRE_USER_AGENT = f"{settings.OPTIMAP_USER_AGENT} openaire"
OPENAIRE_HTTP_TIMEOUT = settings.OPTIMAP_OPENAIRE_HTTP_TIMEOUT
# Exchanges a (monthly) refresh token for a ~1h access token.
# See https://graph.openaire.eu/docs/apis/authentication/
OPENAIRE_TOKEN_EXCHANGE_URL = "https://services.openaire.eu/uoa-user-management/api/users/getAccessToken"


def _resolve_openaire_bearer_token() -> str | None:
    """Resolve the OpenAIRE bearer token, preferring the DB refresh-token flow.

    Priority:
    1. A live access token exchanged from a refresh token stored in the
       ``ServiceToken`` table (``works.harvesting.openaire.get_openaire_access_token``).
    2. The static ``OPTIMAP_OPENAIRE_TOKEN`` personal access token from settings
       (backward compatibility for deployments without a DB row).
    3. ``None`` — anonymous (60 requests/hour).
    """
    try:
        from works.harvesting.openaire import get_openaire_access_token

        access = get_openaire_access_token()
    except Exception as exc:  # noqa: BLE001 — never break a harvest over auth resolution
        logger.warning("OpenAIRE access-token resolution failed: %s", exc)
        access = None
    if access:
        return access
    return settings.OPTIMAP_OPENAIRE_TOKEN or None


def _openaire_session() -> requests.Session:
    """Return a `requests.Session` for the OpenAIRE Graph API.

    Anonymous requests are limited to 60/hour; an authenticated Bearer token
    raises the limit to 7200/hour. The token is resolved by
    ``_resolve_openaire_bearer_token`` (DB refresh-token flow first, then the
    static ``OPTIMAP_OPENAIRE_TOKEN``). Retries respect the upstream
    Retry-After header so 429s back off cleanly.
    """
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    headers = {
        "User-Agent": OPENAIRE_USER_AGENT,
        "Accept": "application/json",
    }
    token = _resolve_openaire_bearer_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    session.headers.update(headers)
    return session


# Mountain Wetlands Repository (MaRESS) ---------------------------------------

MWR_PAGE_SIZE = 500
MWR_HTTP_TIMEOUT = 60  # seconds; MaRESS responses can be hefty (study_sites embed)


def _mwr_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": OAI_USER_AGENT, "Accept": "application/json"})
    return session
