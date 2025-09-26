import json
import os
from pathlib import Path
from typing import Iterable

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

import requests
import markdown  # runtime dependency
from zenodo_client import Zenodo


# --------- helpers kept at module scope so tests can patch them ----------

def _markdown_to_html(markdown_text: str) -> str:
    """Convert README.md markdown to HTML for Zenodo `description`."""
    return markdown.markdown(markdown_text, extensions=["tables", "fenced_code"])


def update_zenodo(
    deposition_id: str,
    paths: list[Path],
    sandbox: bool = True,
    access_token: str | None = None,
):
    """
    Thin wrapper around zenodo_client.Zenodo.update() so tests can patch here.
    Only updates the existing draft (publish=False).
    """
    z = Zenodo(sandbox=sandbox)
    if access_token:
        z.access_token = access_token
    return z.update(deposition_id=deposition_id, paths=[str(p) for p in paths], publish=False)


# ------------------ HTTP / config helpers ------------------

def _api_base() -> str:
    base = os.getenv("ZENODO_API_BASE") or getattr(settings, "ZENODO_API_BASE", "https://sandbox.zenodo.org/api")
    if base.endswith("/"):
        raise SystemExit(f"ZENODO_API_BASE must not end with '/'. Got: {base!r}")
    return base


def _token(explicit_token: str | None = None) -> str:
    """Resolve token from (1) CLI, (2) env, (3) settings. Fail fast if missing."""
    if explicit_token:
        return explicit_token
    token = (
        os.getenv("ZENODO_API_TOKEN")
        or os.getenv("ZENODO_SANDBOX_API_TOKEN")
        or getattr(settings, "ZENODO_API_TOKEN", None)
        or getattr(settings, "ZENODO_SANDBOX_API_TOKEN", None)
        or getattr(settings, "ZENODO_SANDBOX_TOKEN", None)
    )
    if not token:
        raise SystemExit("No Zenodo API token. Set ZENODO_API_TOKEN (or ZENODO_SANDBOX_API_TOKEN).")
    return token


def _get_deposition(api_base: str, token: str, deposition_id: str):
    r = requests.get(
        f"{api_base}/deposit/depositions/{deposition_id}",
        params={"access_token": token},
        timeout=30,
    )
    try:
        rf = getattr(r, "raise_for_status", None)
        if callable(rf):
            rf()
        else:
            # no raise_for_status on mock: fallback to status_code check
            if getattr(r, "status_code", 200) >= 400:
                from requests import HTTPError
                raise HTTPError(f"Bad status {getattr(r, 'status_code', 'n/a')}")
    except Exception as ex:
        status = getattr(r, "status_code", "n/a")
        body = getattr(r, "text", "")
        from django.core.management.base import CommandError
        raise CommandError(f"Failed to fetch deposition {deposition_id}: {status} {body}") from ex
    return r.json()

# ------------------ metadata merging ------------------

_REQ_PRESERVE = {"title", "upload_type", "publication_date", "creators"}  # never overwrite


def _merge_keywords(existing: Iterable[str] | None, incoming: Iterable[str] | None) -> list[str]:
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
    """Merge by (identifier, relation) pair."""
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


def _build_upload_list(data_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for name in ("README.md", "optimap-main.zip"):
        p = data_dir / name
        if p.exists():
            paths.append(p)
    # include dumps if present
    for pat in ("optimap_data_dump_*.geojson", "optimap_data_dump_*.geojson.gz", "optimap_data_dump_*.gpkg"):
        paths.extend(sorted(data_dir.glob(pat)))
    return paths


class Command(BaseCommand):
    help = "Update an existing Zenodo deposition draft with generated files and selectively patched metadata."

    def add_arguments(self, parser):
        parser.add_argument("--deposition-id", dest="deposition_id", help="Existing deposition (draft) ID on Zenodo.")
        parser.add_argument(
            "--patch",
            dest="patch",
            default="description,version,keywords,related_identifiers",
            help="Comma-separated list of metadata fields to patch (others are preserved).",
        )
        parser.add_argument("--merge-keywords", action="store_true", help="Merge incoming keywords with existing.")
        parser.add_argument("--merge-related", action="store_true", help="Merge incoming related_identifiers.")
        parser.add_argument("--no-build", action="store_true", help="(Kept for compatibility; ignored here.)")
        parser.add_argument("--token", dest="token", help="Zenodo API token (overrides env/settings).")

    def handle(self, *args, **opts):
        api_base = _api_base()
        token = _token(opts.get("token"))
        deposition_id = opts.get("deposition_id") or os.getenv("ZENODO_SANDBOX_DEPOSITION_ID")
        if not deposition_id:
            raise SystemExit("No deposition ID. Provide --deposition-id or set ZENODO_SANDBOX_DEPOSITION_ID.")

        self.stdout.write(
            f"Depositing OPTIMAP data dump to {api_base} "
            f"(configured via {'ZENODO_API_BASE env' if os.getenv('ZENODO_API_BASE') else 'settings/default'})"
        )
        self.stdout.write(f"Using deposition ID {deposition_id}")

        # Determine project root for outputs (test-friendly)
        project_root = Path(
            os.getenv("OPTIMAP_PROJECT_ROOT")
            or getattr(settings, "PROJECT_ROOT", Path(__file__).resolve().parents[3])
        )
        data_dir = project_root / "data"
        data_dir.mkdir(exist_ok=True)

        dyn_path = data_dir / "zenodo_dynamic.json"
        if not dyn_path.exists():
            raise CommandError(f"{dyn_path} not found. Run the render step first.")

        incoming = json.loads(dyn_path.read_text(encoding="utf-8"))

        # Load existing deposition (to preserve required fields)
        dep = _get_deposition(api_base, token, str(deposition_id))
        existing_meta = dep.get("metadata", {}) or {}

        # Decide which fields to patch
        fields_to_patch = {x.strip() for x in (opts.get("patch") or "").split(",") if x.strip()}

        merged = dict(existing_meta)  # start from existing
        # never clobber required fields unless explicitly patched
        for req in _REQ_PRESERVE:
            if req in incoming and req not in fields_to_patch:
                incoming.pop(req, None)

        # description from README.md (markdown -> HTML)
        if "description" in fields_to_patch:
            readme_md = (data_dir / "README.md").read_text(encoding="utf-8")
            merged["description"] = _markdown_to_html(readme_md)

        # version / keywords / related / misc
        for key in fields_to_patch - {"description"}:
            if key == "keywords":
                if opts.get("merge_keywords", False):
                    merged["keywords"] = _merge_keywords(existing_meta.get("keywords"), incoming.get("keywords"))
                else:
                    merged["keywords"] = incoming.get("keywords", [])
            elif key == "related_identifiers":
                if opts.get("merge_related", False):
                    merged["related_identifiers"] = _merge_related(
                        existing_meta.get("related_identifiers"), incoming.get("related_identifiers")
                    )
                else:
                    merged["related_identifiers"] = incoming.get("related_identifiers", [])
            else:
                if key in incoming:
                    merged[key] = incoming[key]

        # tiny diff summary
        changed = [k for k in merged.keys() if existing_meta.get(k) != merged.get(k)]
        self.stdout.write(f"Metadata fields changed: {', '.join(changed) if changed else '(none)'}")

        # PUT metadata back
        put_url = f"{api_base}/deposit/depositions/{deposition_id}"
        res = requests.put(
            put_url,
            params={"access_token": token},
            headers={"Content-Type": "application/json"},
            data=json.dumps({"metadata": merged}),
        )
        try:
            res.raise_for_status()
            self.stdout.write("Metadata updated (merged, no clobber).")
        except Exception as ex:
            raise CommandError(f"Failed to update metadata: {res.status_code} {res.text}") from ex

        # Upload files via zenodo_client
        self.stdout.write("Uploading files to existing Zenodo sandbox draft…")
        paths = _build_upload_list(data_dir)
        for p in paths:
            try:
                size = p.stat().st_size
            except Exception:
                size = 0
            self.stdout.write(f" - {p.name} ({size} bytes)")
        resp = update_zenodo(
            deposition_id=str(deposition_id),
            paths=paths,
            sandbox=("sandbox." in api_base),
            access_token=token,
        )

        try:
            html = resp.json().get("links", {}).get("html")
        except Exception:
            html = None
        if html:
            self.stdout.write(self.style.SUCCESS(f"✅ Updated deposition {deposition_id} at {html}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"✅ Updated deposition {deposition_id}"))
