"""
Zenodo data archival functionality for OPTIMAP.

This module handles rendering metadata and depositing data to Zenodo.
"""
import json
import os
import tempfile
import time
import traceback
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import markdown
import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.urls import reverse
from jinja2 import Environment, FileSystemLoader
from zenodo_client import Zenodo

from works.models import Work, Source, ZenodoDepositionLog

User = get_user_model()


# ================== URL/Domain Helpers ==================

def _extract_domain(u: str | None) -> str | None:
    """Extract domain from URL."""
    if not u:
        return None
    try:
        p = urlparse(u)
        netloc = p.netloc or p.path
        return (netloc or "").lower()
    except Exception:
        return None


def _canonical_url(raw: str | None) -> str | None:
    """Normalize URL to https://<host>/<path> with lowercase host."""
    if not raw:
        return None
    u = raw.strip()
    if "://" not in u:
        u = "https://" + u
    p = urlparse(u)
    host = (p.netloc or p.path).lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    path = p.path or ""
    return f"https://{host}{path}"


def _label_from_domain(domain: str) -> str:
    """Return a cleaned label from a domain name."""
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.capitalize() if domain else "Source"


def _clean_label(name: str | None, url: str | None) -> str:
    """Clean source label."""
    n = (name or "").strip()
    domain = _extract_domain(url) or ""
    if n.isdigit() and domain == "optimap.science":
        return "OPTIMAP"
    if n and not n.isdigit():
        return n
    return _label_from_domain(domain) if domain else "Source"


def _resolve_api_base(api_base: str | None = None) -> str:
    """Resolve the Zenodo API base URL with the same env/settings/default
    cascade that `deposit_to_zenodo` uses, so render and deposit always
    look at the same target when scoping per-target state (e.g. version).
    """
    if api_base is not None:
        return api_base
    return (
        os.getenv("ZENODO_API_BASE")
        or getattr(settings, "ZENODO_API_BASE", "https://sandbox.zenodo.org/api")
    )


def _next_version_for(api_base: str) -> str:
    """
    Compute the next `vN` label by reading the latest successful
    `ZenodoDepositionLog.version` for `api_base`. Sandbox and production
    have separate counters because they target different deposits; a
    failed deposit doesn't burn a version number.
    """
    last = (
        ZenodoDepositionLog.objects
        .filter(status="success", api_base=api_base)
        .exclude(version__isnull=True)
        .exclude(version="")
        .order_by("-deposition_date")
        .values_list("version", flat=True)
        .first()
    )
    last_n = 0
    if last:
        try:
            last_n = int(last.lstrip("v") or 0)
        except ValueError:
            last_n = 0
    return f"v{last_n + 1}"


def _live_download_related_identifiers() -> list[dict]:
    """
    Build Zenodo `related_identifiers` entries pointing at the always-current
    download endpoints on optimap.science. The Zenodo deposit is a frozen
    snapshot; the live URLs serve the rolling release of the same dataset.
    """
    base = settings.BASE_URL.rstrip("/")
    routes = [
        ("optimap:download_geojson", "dataset"),
        ("optimap:download_geopackage", "dataset"),
        ("optimap:download_csv", "dataset"),
    ]
    return [
        {
            "scheme": "url",
            "identifier": f"{base}{reverse(name)}",
            "relation": "isSupplementTo",
            "resource_type": resource_type,
        }
        for name, resource_type in routes
    ]


def _source_identifier(source: dict) -> tuple[str, str] | None:
    """
    Pick the best Zenodo `(scheme, identifier)` for a Source row.

    Preference order: linking ISSN, then journal homepage URL, then the
    harvest endpoint URL. Returns ``None`` for self-references to
    optimap.science (the portal isn't a source it describes) and for
    sources that expose no usable identifier.
    """
    issn = (source.get("issn_l") or "").strip()
    if issn:
        return ("issn", issn)
    for raw in (source.get("homepage_url"), source.get("url_field")):
        url = _canonical_url(raw)
        if not url:
            continue
        if _extract_domain(url) == "optimap.science":
            continue
        return ("url", url)
    return None


# OPTIMAP's grants for the Zenodo deposit. Funder DOIs are Crossref-registered
# IDs (BMBF 10.13039/501100002347; BMFTR uses the same Crossref entity until
# the 2025 rename propagates — we still keep both labels for the free-text
# fallback). The 2025-08-21 issue comment on #63 settled on KOMET + OPTIMETA
# only; NFDI4Earth is intentionally excluded.
#
# Zenodo's legacy deposit API accepts grants as `[{"id": "<funder_doi>::<grant_id>"}]`,
# but it only resolves IDs that are in its curated grants vocabulary. If a
# grant isn't there, the metadata PUT returns 400 — we catch that below and
# fall back to a free-text `notes` entry so the funding info isn't lost.
_FUNDING = [
    {
        "id": "10.13039/501100002347::16TOA028B",
        "name": "OPTIMETA",
        "funder": "BMBF",
        "grant": "16TOA028B",
    },
    {
        "id": "10.13039/501100002347::16KOA009A",
        "name": "KOMET",
        "funder": "BMFTR",
        "grant": "16KOA009A",
    },
]


def _grants_payload() -> list[dict]:
    """Zenodo-compatible grants list — only the `id` key."""
    return [{"id": g["id"]} for g in _FUNDING]


def _funding_fallback_text() -> str:
    """Human-readable funding statement for `metadata.notes` when Zenodo
    can't resolve the structured grant IDs."""
    parts = [f"{g['name']} ({g['funder']} grant {g['grant']})" for g in _FUNDING]
    return "Funding: " + ", ".join(parts) + "."


# Static "Note" description that documents the license split. Wording follows
# the 2025-07-21 issue comment on #63 — both licenses are listed on the
# Zenodo record, the data files are CC0 and only the software snapshot is
# GPLv3, so harvesters and reusers can apply the correct terms per file.
_LICENSE_NOTE_HTML = (
    "<p><strong>Mixed licenses:</strong> this record bundles data files and a "
    "snapshot of the OPTIMAP source code, which carry different licenses.</p>"
    "<ul>"
    "<li>The <strong>data files</strong> "
    "(<code>README.md</code>, <code>optimap_data_dump_*.geojson</code>, "
    "<code>optimap_data_dump_*.geojson.gz</code>, "
    "<code>optimap_data_dump_*.gpkg</code>, "
    "<code>optimap_data_dump_*.csv</code>, "
    "<code>optimap_data_dump_*.csv.gz</code>) "
    "are published under the "
    "<a href=\"https://creativecommons.org/publicdomain/zero/1.0/\">"
    "Creative Commons Zero (CC0-1.0)</a> license.</li>"
    "<li>The <strong>software snapshot</strong> "
    "(<code>optimap-main.zip</code>) is published under the "
    "<a href=\"https://opensource.org/licenses/GPL-3.0\">"
    "GNU General Public License v3.0 (GPL-3.0)</a>.</li>"
    "</ul>"
)


def _license_additional_descriptions() -> list[dict]:
    """
    Build the Zenodo `additional_descriptions` entry that documents the
    CC0 (data) / GPL-3.0 (code snapshot) license split.
    """
    return [{"type": "notes", "description": _LICENSE_NOTE_HTML}]


def _describes_related_identifiers(sources: Iterable[dict]) -> list[dict]:
    """
    One Zenodo `related_identifiers` entry per harvested Source with
    relation=describes, resource_type=publication — i.e. "this record
    describes Journal X". Wording follows the 2025-07-14 issue comment
    on #63.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for s in sources:
        ident = _source_identifier(s)
        if ident is None or ident in seen:
            continue
        seen.add(ident)
        scheme, value = ident
        out.append({
            "scheme": scheme,
            "identifier": value,
            "relation": "describes",
            "resource_type": "publication",
        })
    return out


# ================== Rendering ==================

def render_zenodo_package(
    project_root: Path | None = None,
    stdout_callback=None,
    api_base: str | None = None,
) -> dict:
    """
    Render Zenodo data package (README, metadata, archive).

    Returns dict with paths to generated files.

    `api_base` scopes the version counter so sandbox and production
    increment independently. Defaults to the same env/settings cascade
    that `deposit_to_zenodo` uses.
    """
    def log(msg):
        if stdout_callback:
            stdout_callback(msg)

    # Determine project root
    if project_root is None:
        project_root = Path(
            os.getenv("OPTIMAP_PROJECT_ROOT")
            or getattr(settings, "PROJECT_ROOT", Path(__file__).resolve().parents[1])
        )

    data_dir = project_root / "data"
    data_dir.mkdir(exist_ok=True)

    # Version: source of truth is the latest successful ZenodoDepositionLog
    # for this api_base. A tracked file would drift across environments and
    # silently restart at v1 on a fresh checkout.
    api_base = _resolve_api_base(api_base)
    version = _next_version_for(api_base)

    # Zip snapshot — the deposit must include a copy of the OPTIMAP source
    # tree (issue #63, last checklist item). A silent empty-zip fallback
    # would upload a 0-byte optimap-main.zip and look like a successful
    # deposit, so failures here propagate as a CommandError-friendly
    # RuntimeError instead.
    archive_path = data_dir / "optimap-main.zip"
    log(f"Generating {archive_path.name}...")
    import subprocess
    try:
        result = subprocess.run(
            ["git", "archive", "--format=zip", "HEAD", "-o", str(archive_path)],
            cwd=str(project_root),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as ex:
        raise RuntimeError(
            "Cannot produce optimap-main.zip: the `git` binary is not on PATH"
        ) from ex
    except subprocess.CalledProcessError as ex:
        raise RuntimeError(
            f"`git archive HEAD` failed (exit {ex.returncode}) in {project_root}: "
            f"{(ex.stderr or '').strip()}"
        ) from ex
    if not archive_path.exists() or archive_path.stat().st_size == 0:
        raise RuntimeError(
            f"`git archive HEAD` produced no archive at {archive_path}; "
            f"stderr={(result.stderr or '').strip()!r}"
        )

    # Gather statistics
    article_count = Work.objects.count()
    spatial_count = Work.objects.exclude(geometry=None).count()
    temporal_count = Work.objects.exclude(timeperiod_startdate=None).count()
    earliest_date = (
        Work.objects.order_by("publicationDate").values_list("publicationDate", flat=True).first() or ""
    )
    latest_date = (
        Work.objects.order_by("-publicationDate").values_list("publicationDate", flat=True).first() or ""
    )

    # Sources for the README — dedupe by canonical domain so the same
    # publisher doesn't appear twice in the visible list.
    source_rows = list(
        Source.objects.all().values("name", "url_field", "homepage_url", "issn_l")
    )
    seen_domains: set[str] = set()
    sources: list[dict] = []
    for s in source_rows:
        url = _canonical_url(s.get("url_field"))
        dom = _extract_domain(url)
        if not dom or dom in seen_domains:
            continue
        seen_domains.add(dom)
        sources.append({"name": _clean_label(s.get("name"), url), "url": url})

    # Render README.md
    tmpl_dir = project_root / "works" / "templates"
    env = Environment(loader=FileSystemLoader(str(tmpl_dir)), trim_blocks=True, lstrip_blocks=True)
    template = env.get_template("README.md.j2")
    rendered = template.render(
        version=version,
        date=date.today().isoformat(),
        article_count=article_count,
        sources=sources,
        spatial_count=spatial_count,
        temporal_count=temporal_count,
        earliest_date=earliest_date,
        latest_date=latest_date,
    )
    readme_path = data_dir / "README.md"
    readme_path.write_text(rendered, encoding="utf-8")

    # Dynamic metadata
    dyn_path = data_dir / "zenodo_dynamic.json"
    existing_dyn = {}
    if dyn_path.exists():
        try:
            existing_dyn = json.loads(dyn_path.read_text(encoding="utf-8"))
        except Exception:
            existing_dyn = {}

    # Final keyword list per nuest's 2025-07-14 comment on #63. "Open Research
    # Information" and its short form "ORI" both appear so the record is
    # discoverable under either label.
    default_keywords = [
        "Open Access",
        "Open Science",
        "Open Research Information",
        "ORI",
        "Open Data",
        "FAIR",
    ]
    # Contributor-level attribution is deferred to #207; for now the deposit's
    # creator is the project as a whole, matching the 2025-07-14 decision.
    default_creators = existing_dyn.get("creators") or [
        {"name": "OPTIMAP Contributors", "affiliation": "OPTIMAP Project"}
    ]

    # `related_identifiers` is always derived from current state — the live
    # download URLs come from settings.BASE_URL + URL config, and the
    # "describes" entries are recomputed from the Source table on every run.
    # A stale zenodo_dynamic.json from another environment cannot leak in.
    related_identifiers = [
        *_live_download_related_identifiers(),
        *_describes_related_identifiers(source_rows),
    ]

    dyn = {
        **existing_dyn,
        "title": existing_dyn.get("title") or "OPTIMAP FAIR Data Package",
        "upload_type": existing_dyn.get("upload_type") or "dataset",
        "publication_date": date.today().isoformat(),
        "creators": default_creators,
        "version": version,
        "keywords": existing_dyn.get("keywords") or default_keywords,
        "related_identifiers": related_identifiers,
        "additional_descriptions": _license_additional_descriptions(),
        "grants": _grants_payload(),
        "description_markdown": readme_path.read_text(encoding="utf-8"),
    }
    dyn_path.write_text(json.dumps(dyn, indent=2), encoding="utf-8")

    log(f"Generated: {archive_path.name}, {readme_path.name}, {dyn_path.name}")

    return {
        "version": version,
        "archive_path": archive_path,
        "readme_path": readme_path,
        "metadata_path": dyn_path,
        "data_dir": data_dir,
    }


# ================== Deposition ==================

_REQ_PRESERVE = {"doi", "prereserve_doi"}  # never overwrite


def _markdown_to_html(markdown_text: str) -> str:
    """Convert README.md markdown to HTML for Zenodo description."""
    return markdown.markdown(markdown_text, extensions=["tables", "fenced_code"])


def _merge_keywords(existing: Iterable[str] | None, incoming: Iterable[str] | None) -> list[str]:
    """Merge keyword lists without duplicates."""
    seen, out = set(), []
    for x in (existing or []):
        if x not in seen:
            seen.add(x)
            out.append(x)
    for x in (incoming or []):
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _merge_related(existing: Iterable[dict] | None, incoming: Iterable[dict] | None) -> list[dict]:
    """Merge related_identifiers by (identifier, relation) pair."""
    def key(d: dict) -> tuple[str, str]:
        return (d.get("identifier", ""), d.get("relation", ""))

    seen, out = set(), []
    for d in (existing or []):
        k = key(d)
        if k not in seen:
            seen.add(k)
            out.append(d)
    for d in (incoming or []):
        k = key(d)
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def _get_deposition(api_base: str, token: str, deposition_id: str) -> dict:
    """Fetch existing deposition from Zenodo API."""
    r = requests.get(
        f"{api_base}/deposit/depositions/{deposition_id}",
        params={"access_token": token},
        timeout=30,
    )
    try:
        r.raise_for_status()
    except Exception as ex:
        raise Exception(f"Failed to fetch deposition {deposition_id}: {r.status_code} {r.text}") from ex
    return r.json()


def _is_published(dep: dict) -> bool:
    """
    Zenodo marks a published deposition with ``submitted=true`` and ``state="done"``.
    Drafts (`unsubmitted` / `inprogress`) are still editable; published records
    require a `newversion` call before we can change anything.
    """
    return bool(dep.get("submitted")) and dep.get("state") == "done"


def _extract_id_from_url(url: str | None) -> str | None:
    """Pull the trailing numeric ID off a Zenodo deposition URL."""
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def _create_new_draft(api_base: str, token: str) -> str:
    """
    POST /deposit/depositions with an empty body — creates a fresh draft and
    returns its numeric ID. Used to bootstrap the very first deposit when no
    deposition_id is configured and no prior log exists for this api_base.
    """
    r = requests.post(
        f"{api_base}/deposit/depositions",
        params={"access_token": token},
        headers={"Content-Type": "application/json"},
        data=json.dumps({}),
        timeout=30,
    )
    try:
        r.raise_for_status()
    except Exception as ex:
        raise Exception(
            f"Failed to create new Zenodo draft: {r.status_code} {r.text}"
        ) from ex
    payload = r.json()
    new_id = payload.get("id") or _extract_id_from_url(
        payload.get("links", {}).get("self")
    )
    if not new_id:
        raise Exception(
            f"Zenodo create-draft response did not include an id: {payload!r}"
        )
    return str(new_id)


def _create_new_version(api_base: str, token: str, deposition_id: str) -> str:
    """
    POST /deposit/depositions/{id}/actions/newversion — fork a new editable
    draft off a published deposition. The response carries the new draft URL
    in `links.latest_draft` (Zenodo legacy API); the new ID is the trailing
    numeric segment. The new draft inherits files and metadata from the
    published version; the caller is expected to delete the inherited files
    and re-PUT updated metadata, which the existing deposit flow already
    does.
    """
    r = requests.post(
        f"{api_base}/deposit/depositions/{deposition_id}/actions/newversion",
        params={"access_token": token},
        timeout=30,
    )
    try:
        r.raise_for_status()
    except Exception as ex:
        raise Exception(
            f"Failed to create new version of deposition {deposition_id}: "
            f"{r.status_code} {r.text}"
        ) from ex
    payload = r.json()
    new_url = payload.get("links", {}).get("latest_draft")
    new_id = _extract_id_from_url(new_url)
    if not new_id:
        raise Exception(
            f"newversion response for {deposition_id} did not include "
            f"a latest_draft link: {payload!r}"
        )
    return str(new_id)


def _latest_log_deposition_id(api_base: str) -> str | None:
    """
    Most-recent successful ZenodoDepositionLog deposition_id for the given
    api_base. Used to recover the current draft / latest-published ID when
    no explicit env/setting deposition_id is configured — so scheduled and
    re-triggered runs land on the same record without manual env edits.
    """
    return (
        ZenodoDepositionLog.objects
        .filter(status="success", api_base=api_base)
        .exclude(deposition_id__isnull=True)
        .exclude(deposition_id="")
        .order_by("-deposition_date")
        .values_list("deposition_id", flat=True)
        .first()
    )


_DUMP_PATTERNS = (
    "optimap_data_dump_*.geojson",
    "optimap_data_dump_*.geojson.gz",
    "optimap_data_dump_*.gpkg",
    "optimap_data_dump_*.csv",
    "optimap_data_dump_*.csv.gz",
)


def _dump_timestamp(p: Path) -> str:
    """
    Extract the timestamp portion of an `optimap_data_dump_<TS>.<ext>` filename.
    Returns "" for non-matching paths.
    """
    name = p.name
    if not name.startswith("optimap_data_dump_"):
        return ""
    # Strip leading prefix and trailing suffix (everything from the first '.')
    stem = name[len("optimap_data_dump_"):]
    return stem.split(".", 1)[0]


def _latest_dump_files(directory: Path) -> list[Path]:
    """
    Return all dump files belonging to the newest timestamp present in
    `directory`, across geojson / geojson.gz / gpkg / csv / csv.gz. Old
    cycles are ignored so a deposit never ships stale formats next to
    fresh ones.
    """
    if not directory.exists():
        return []
    candidates: list[Path] = []
    for pat in _DUMP_PATTERNS:
        candidates.extend(directory.glob(pat))
    if not candidates:
        return []
    latest = max(_dump_timestamp(p) for p in candidates)
    return sorted(p for p in candidates if _dump_timestamp(p) == latest)


def _build_upload_list(data_dir: Path, dump_dir: Path | None = None) -> list[Path]:
    """
    Build the file list for a Zenodo deposit.

    - `README.md` and `optimap-main.zip` come from `data_dir` (where the
      render step writes them).
    - Data dumps come from `data_dir` first (covers tests and ad-hoc
      single-directory layouts); falling back to `dump_dir`, which
      defaults to the `optimap_cache` directory `regenerate_data_dumps`
      writes to in production.
    """
    if dump_dir is None:
        dump_dir = Path(tempfile.gettempdir()) / "optimap_cache"

    paths: list[Path] = []
    for name in ("README.md", "optimap-main.zip"):
        p = data_dir / name
        if p.exists():
            paths.append(p)

    dumps = _latest_dump_files(data_dir)
    if not dumps and data_dir.resolve() != dump_dir.resolve():
        dumps = _latest_dump_files(dump_dir)
    paths.extend(dumps)
    return paths


def _send_admin_notification(log_entry: ZenodoDepositionLog, stdout_callback=None):
    """Send email notification to all admin users."""
    admin_emails = list(User.objects.filter(is_staff=True, is_active=True).values_list('email', flat=True))

    if not admin_emails:
        if stdout_callback:
            stdout_callback("No admin users found to notify")
        return

    # Build email
    if log_entry.status == 'success':
        subject = f'✅ Zenodo Deposition Successful - {log_entry.version or log_entry.deposition_id}'
        status_emoji = '✅'
        status_text = 'SUCCESS'
    else:
        subject = f'❌ Zenodo Deposition Failed - {log_entry.deposition_id}'
        status_emoji = '❌'
        status_text = 'FAILED'

    files_text = "\n".join([
        f"  • {f['name']} ({f['size']:,} bytes)"
        for f in log_entry.files_uploaded
    ]) if log_entry.files_uploaded else "  (none)"

    duration_text = "N/A"
    if log_entry.upload_duration_seconds:
        minutes = int(log_entry.upload_duration_seconds // 60)
        seconds = int(log_entry.upload_duration_seconds % 60)
        duration_text = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

    message_parts = [
        f"{status_emoji} ZENODO DEPOSITION {status_text}",
        "=" * 70,
        "",
        f"Deposition ID: {log_entry.deposition_id}",
        f"Version: {log_entry.version or 'N/A'}",
        f"API Base: {log_entry.api_base}",
        f"Date: {log_entry.deposition_date.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"Duration: {duration_text}",
        "",
    ]

    if log_entry.status == 'success':
        message_parts.extend([
            f"Works Included: {log_entry.works_count:,}",
            f"Files Uploaded: {len(log_entry.files_uploaded) if log_entry.files_uploaded else 0}",
            f"Total Size: {log_entry.total_size_bytes:,} bytes",
            "",
            "Files:",
            files_text,
            "",
        ])

        if log_entry.zenodo_url:
            message_parts.extend([
                "⚠️  ACTION REQUIRED ⚠️",
                "",
                "The deposition is in DRAFT state and not yet published.",
                "Please review and publish manually:",
                "",
                f"  {log_entry.zenodo_url}",
                "",
                "⚠️  Publishing cannot be undone!",
                "",
            ])

        if log_entry.doi:
            message_parts.append(f"DOI: {log_entry.doi}")

        if log_entry.deposition_summary:
            message_parts.extend(["", "Summary:", f"  {log_entry.deposition_summary}"])
    else:
        message_parts.extend([
            "ERROR:",
            f"  {log_entry.error_message or 'Unknown error'}",
            "",
        ])

        if log_entry.error_details:
            message_parts.extend([
                "Error Details:",
                f"  Type: {log_entry.error_details.get('exception_type', 'N/A')}",
                "",
            ])

            if 'traceback' in log_entry.error_details:
                message_parts.extend([
                    "Traceback:",
                    log_entry.error_details['traceback'],
                ])

    message_parts.extend([
        "",
        "=" * 70,
        "",
    ])

    site_url = getattr(settings, 'SITE_URL', None)
    if site_url:
        message_parts.append(f"View full log: {site_url}/admin/works/zenododepositionlog/{log_entry.id}/change/")
    else:
        message_parts.append(f"View full log in admin: /admin/works/zenododepositionlog/{log_entry.id}/change/")

    message_parts.extend([
        "",
        "This is an automated message from OPTIMAP.",
    ])

    message = "\n".join(message_parts)

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=admin_emails,
            fail_silently=False,
        )
        if stdout_callback:
            stdout_callback(f"Admin notification sent to {len(admin_emails)} admin(s)")
    except Exception as ex:
        if stdout_callback:
            stdout_callback(f"Warning: Failed to send admin notification: {ex}")


def deposit_to_zenodo(
    deposition_id: str | None = None,
    api_base: str | None = None,
    token: str | None = None,
    patch_fields: str | None = None,
    merge_keywords: bool = False,
    merge_related: bool = False,
    project_root: Path | None = None,
    stdout_callback=None,
) -> ZenodoDepositionLog:
    """
    Deposit rendered files to Zenodo.

    Resolution / bootstrap flow for ``deposition_id``:

    1. Explicit argument wins.
    2. Else fall back to the latest successful ZenodoDepositionLog for this
       ``api_base`` — so scheduled and re-triggered runs find the same draft
       (or the previously published record, see step 4) without manual env
       edits.
    3. Else POST /deposit/depositions to bootstrap a fresh draft.
    4. After resolving the ID, GET the deposition. If it's already published
       (``submitted=true`` AND ``state="done"``), POST .../actions/newversion
       to fork an editable draft and target *that* instead — issue #63 only
       requires manual *publication*, so the next deposit cycle should
       silently start the next version.

    Args:
        deposition_id: Zenodo deposition ID (optional — resolved/bootstrapped
            when omitted, per the flow above).
        api_base: Zenodo API base URL (default: from settings)
        token: Zenodo API token (default: from settings/env)
        patch_fields: Comma-separated fields to update (default: description,version,keywords,related_identifiers)
        merge_keywords: Merge keywords instead of replacing
        merge_related: Merge related_identifiers instead of replacing
        project_root: Project root directory
        stdout_callback: Callback for logging messages

    Returns:
        ZenodoDepositionLog entry
    """
    def log(msg):
        if stdout_callback:
            stdout_callback(msg)

    # Resolve API base
    if api_base is None:
        api_base = os.getenv("ZENODO_API_BASE") or getattr(settings, "ZENODO_API_BASE", "https://sandbox.zenodo.org/api")

    if api_base.endswith("/"):
        raise ValueError(f"ZENODO_API_BASE must not end with '/'. Got: {api_base!r}")

    # Resolve token
    if token is None:
        token = (
            os.getenv("ZENODO_API_TOKEN")
            or os.getenv("ZENODO_SANDBOX_API_TOKEN")
            or getattr(settings, "ZENODO_API_TOKEN", None)
            or getattr(settings, "ZENODO_SANDBOX_API_TOKEN", None)
        )

    if not token:
        raise ValueError("No Zenodo API token. Set ZENODO_API_TOKEN or provide token parameter.")

    # Determine project root
    if project_root is None:
        project_root = Path(
            os.getenv("OPTIMAP_PROJECT_ROOT")
            or getattr(settings, "PROJECT_ROOT", Path(__file__).resolve().parents[1])
        )

    data_dir = project_root / "data"

    # Resolve deposition_id: explicit arg → latest successful log for this
    # api_base → bootstrap a fresh draft. Done before log_entry creation so
    # the log row records the *actual* target ID even on bootstrap.
    bootstrapped = False
    deposition_id_str = str(deposition_id) if deposition_id else ""
    if not deposition_id_str:
        recovered = _latest_log_deposition_id(api_base)
        if recovered:
            log(f"No deposition_id supplied; reusing latest from log: {recovered}")
            deposition_id_str = recovered
        else:
            log("No deposition_id supplied and no prior log; creating new draft...")
            deposition_id_str = _create_new_draft(api_base, token)
            bootstrapped = True
            log(f"Created new draft {deposition_id_str}")

    # Initialize log
    log_entry = ZenodoDepositionLog(
        deposition_id=deposition_id_str,
        api_base=api_base,
        status='failed',
    )

    log_entry.works_count = Work.objects.count()

    upload_start = time.time()

    try:
        # Load metadata
        dyn_path = data_dir / "zenodo_dynamic.json"
        if not dyn_path.exists():
            raise FileNotFoundError(f"{dyn_path} not found. Run render_zenodo_package() first.")

        incoming = json.loads(dyn_path.read_text(encoding="utf-8"))

        # Version: written into the rendered metadata by render_zenodo_package
        # — the previous file-based tracker (data/last_version.txt) was
        # removed in favour of ZenodoDepositionLog as source of truth.
        version_str = (incoming.get("version") or "").strip()
        if version_str:
            log_entry.version = version_str

        # Fetch existing deposition (skip when we just bootstrapped it — the
        # POST response would already be a known-good empty draft, but the
        # GET keeps the rest of the flow uniform).
        dep = _get_deposition(api_base, token, deposition_id_str)

        # New-version handoff: if the targeted record is already published,
        # fork a new draft and switch to it before patching/uploading.
        if _is_published(dep):
            log(
                f"Deposition {deposition_id_str} is already published; "
                "creating a new version draft..."
            )
            deposition_id_str = _create_new_version(api_base, token, deposition_id_str)
            log_entry.deposition_id = deposition_id_str
            log(f"New version draft: {deposition_id_str}")
            dep = _get_deposition(api_base, token, deposition_id_str)

        existing_meta = dep.get("metadata", {}) or {}

        # Determine fields to patch
        if patch_fields is None:
            patch_fields = (
                "description,version,keywords,related_identifiers,"
                "additional_descriptions,grants,title,upload_type,"
                "publication_date,creators"
            )

        fields_to_patch = {x.strip() for x in patch_fields.split(",") if x.strip()}

        merged = dict(existing_meta)

        # Remove protected fields from incoming
        for req in _REQ_PRESERVE:
            if req in incoming and req not in fields_to_patch:
                incoming.pop(req, None)

        # Update description from README
        if "description" in fields_to_patch:
            readme_md = (data_dir / "README.md").read_text(encoding="utf-8")
            merged["description"] = _markdown_to_html(readme_md)

        # Update other fields
        for key in fields_to_patch - {"description"}:
            if key == "keywords":
                if merge_keywords:
                    merged["keywords"] = _merge_keywords(existing_meta.get("keywords"), incoming.get("keywords"))
                else:
                    merged["keywords"] = incoming.get("keywords", [])
            elif key == "related_identifiers":
                if merge_related:
                    merged["related_identifiers"] = _merge_related(
                        existing_meta.get("related_identifiers"), incoming.get("related_identifiers")
                    )
                else:
                    merged["related_identifiers"] = incoming.get("related_identifiers", [])
            else:
                if key in incoming:
                    merged[key] = incoming[key]

        # Track changes
        changed = [k for k in merged.keys() if existing_meta.get(k) != merged.get(k)]
        log(f"Metadata fields changed: {', '.join(changed) if changed else '(none)'}")

        log_entry.metadata_merged = {k: merged[k] for k in changed} if changed else {}

        # PUT metadata — with a one-shot fallback for the curated `grants`
        # vocabulary. Zenodo only resolves grants in its preloaded list; if a
        # specific BMBF/BMFTR ID isn't there yet, the API returns 400 and we
        # retry once with `grants` removed and the funding info moved to a
        # free-text `notes` paragraph so the deposit still succeeds.
        put_url = f"{api_base}/deposit/depositions/{deposition_id_str}"

        def _put(payload: dict):
            return requests.put(
                put_url,
                params={"access_token": token},
                headers={"Content-Type": "application/json"},
                data=json.dumps({"metadata": payload}),
            )

        res = _put(merged)
        if res.status_code == 400 and "grants" in merged and "grants" in res.text.lower():
            fallback = _funding_fallback_text()
            log(
                "Zenodo rejected the structured grants metadata; "
                "falling back to free-text in `notes`."
            )
            del merged["grants"]
            existing_notes = (merged.get("notes") or "").strip()
            merged["notes"] = (
                f"{existing_notes}\n\n{fallback}".strip() if existing_notes else fallback
            )
            log_entry.notes = (
                (log_entry.notes + "\n" if log_entry.notes else "")
                + f"[fallback] {fallback}"
            )
            res = _put(merged)
        res.raise_for_status()
        log("Metadata updated.")

        # Delete existing files
        log("Deleting existing files...")
        existing_files = dep.get("files", [])
        for file_obj in existing_files:
            file_id = file_obj.get("id")
            if file_id:
                delete_url = f"{api_base}/deposit/depositions/{deposition_id_str}/files/{file_id}"
                del_res = requests.delete(delete_url, params={"access_token": token})
                if del_res.status_code == 204:
                    log(f" - Deleted: {file_obj.get('filename')}")
                else:
                    log(f" - Failed to delete {file_obj.get('filename')}: {del_res.status_code}")

        # Upload files
        log("Uploading files...")
        paths = _build_upload_list(data_dir)

        files_info = []
        total_size = 0
        for p in paths:
            try:
                size = p.stat().st_size
                total_size += size
                files_info.append({"name": p.name, "size": size})
            except Exception:
                size = 0
                files_info.append({"name": p.name, "size": 0})
            log(f" - {p.name} ({size} bytes)")

        log_entry.files_uploaded = files_info
        log_entry.total_size_bytes = total_size

        # Use zenodo_client for upload
        z = Zenodo(sandbox=("sandbox." in api_base))
        z.access_token = token
        resp = z.update(deposition_id=deposition_id_str, paths=[str(p) for p in paths], publish=False)

        upload_duration = time.time() - upload_start
        log_entry.upload_duration_seconds = upload_duration

        # Extract response data
        try:
            resp_data = resp.json()
            html = resp_data.get("links", {}).get("html")
            doi = resp_data.get("doi")

            if html:
                log_entry.zenodo_url = html
            if doi:
                log_entry.doi = doi
        except Exception:
            html = None

        # Mark success
        log_entry.status = 'success'
        bootstrap_note = " (bootstrapped a new draft)" if bootstrapped else ""
        log_entry.deposition_summary = (
            f"Successfully uploaded {len(files_info)} files "
            f"({_format_bytes(total_size)}) to Zenodo deposition {deposition_id_str}{bootstrap_note}. "
            f"Updated metadata fields: {', '.join(changed) if changed else '(none)'}. "
            f"Upload duration: {upload_duration:.2f}s"
        )

        if html:
            log(f"✅ Updated deposition {deposition_id_str} at {html}")
        else:
            log(f"✅ Updated deposition {deposition_id_str}")

    except Exception as ex:
        log_entry.status = 'failed'
        log_entry.error_message = str(ex)
        log_entry.error_details = {
            "exception_type": type(ex).__name__,
            "traceback": traceback.format_exc(),
        }
        log_entry.upload_duration_seconds = time.time() - upload_start
        log_entry.deposition_summary = f"Failed to upload to Zenodo: {str(ex)}"

        log_entry.save()
        _send_admin_notification(log_entry, stdout_callback)
        raise

    # Save and notify
    log_entry.save()
    log(f"Deposition log saved (ID: {log_entry.id})")
    _send_admin_notification(log_entry, stdout_callback)

    return log_entry


def _format_bytes(size_bytes: int) -> str:
    """Format bytes in human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"
