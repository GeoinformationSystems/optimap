# publications/management/commands/deploy_zenodo.py
import json
import os
from pathlib import Path

import requests
from django.conf import settings
from django.core.management import BaseCommand, call_command


def _markdown_to_html(text: str) -> str:
    """Convert Markdown to HTML; fall back to minimal conversion if markdown isn't installed."""
    try:
        import markdown  # optional dependency
        return markdown.markdown(text)
    except Exception:
        esc = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return "<p>" + esc.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


# ------------------------------
# HTTP helpers (robust to test doubles)
# ------------------------------

def _ensure_ok(res):
    """Work with real requests.Response or a simple test double that may lack raise_for_status()."""
    if hasattr(res, "raise_for_status"):
        res.raise_for_status()
        return
    code = getattr(res, "status_code", 200)
    if code is None:
        return
    if int(code) >= 400:
        raise RuntimeError(f"HTTP error {code}")

def _to_json(res) -> dict:
    """Get JSON from real response or from a test double with .text or an injected ._json."""
    if hasattr(res, "json"):
        return res.json()
    if hasattr(res, "text"):
        try:
            return json.loads(res.text or "{}")
        except Exception:
            return {}
    data = getattr(res, "_json", None)
    return data if isinstance(data, dict) else {}


# ------------------------------
# Zenodo API primitives
# ------------------------------

def _api_base() -> str:
    return getattr(
        settings,
        "ZENODO_API_BASE",
        os.getenv("ZENODO_API_BASE", "https://sandbox.zenodo.org/api"),
    ).rstrip("/")

def _token() -> str:
    tok = getattr(settings, "ZENODO_SANDBOX_TOKEN", None) or os.getenv("ZENODO_API_TOKEN")
    if not tok:
        raise SystemExit("No Zenodo API token. Set settings.ZENODO_SANDBOX_TOKEN or ZENODO_API_TOKEN.")
    return tok

def _get_deposition(api_base: str, token: str, deposition_id: str) -> dict:
    r = requests.get(
        f"{api_base}/deposit/depositions/{deposition_id}",
        params={"access_token": token},
    )
    _ensure_ok(r)
    return _to_json(r)

def _post_edit(api_base: str, token: str, deposition_id: str) -> None:
    r = requests.post(
        f"{api_base}/deposit/depositions/{deposition_id}/actions/edit",
        params={"access_token": token},
    )
    _ensure_ok(r)

def _put_metadata(api_base: str, token: str, deposition_id: str, metadata: dict) -> None:
    headers = {"Content-Type": "application/json"}
    data = json.dumps({"metadata": metadata})
    r = requests.put(
        f"{api_base}/deposit/depositions/{deposition_id}",
        params={"access_token": token},
        headers=headers,
        data=data,
    )
    _ensure_ok(r)

def _upload_files(api_base: str, token: str, deposition: dict, paths: list[Path]) -> None:
    bucket = deposition.get("links", {}).get("bucket")
    if not bucket:
        raise SystemExit("No bucket link on deposition; cannot upload files.")
    for p in paths:
        with open(p, "rb") as fh:
            r = requests.put(f"{bucket}/{p.name}", params={"access_token": token}, data=fh)
            _ensure_ok(r)


# ------------------------------
# Merge helpers for patching
# ------------------------------

def _merge_list_unique(existing: list[str], incoming: list[str]) -> list[str]:
    seen, out = set(), []
    for x in (existing or []) + (incoming or []):
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def _merge_related(existing: list[dict], incoming: list[dict]) -> list[dict]:
    key = lambda d: (d.get("identifier"), d.get("relation"), d.get("scheme"))
    seen, out = set(), []
    for d in (existing or []) + (incoming or []):
        k = key(d)
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


# ------------------------------
# Locate latest generated dumps
# ------------------------------

def _find_latest_dump_files() -> list[Path]:
    tmp = Path(os.getenv("TMPDIR", "/tmp")) / "optimap_cache"
    if not tmp.exists():
        return []
    candidates = list(tmp.glob("optimap_data_dump_*.geojson")) + \
                 list(tmp.glob("optimap_data_dump_*.geojson.gz")) + \
                 list(tmp.glob("optimap_data_dump_*.gpkg"))
    by_ext: dict[str, Path] = {}
    for p in candidates:
        key = p.suffix if p.suffix != ".gz" else ".geojson.gz"
        if key not in by_ext or p.stat().st_mtime > by_ext[key].stat().st_mtime:
            by_ext[key] = p
    return [p for p in by_ext.values() if p.exists()]


# ------------------------------
# Compatibility shim used by tests (also OK in prod)
# ------------------------------

def update_zenodo(deposition_id: str,
                  paths: list[Path],
                  sandbox: bool = True,
                  access_token: str | None = None,
                  publish: bool = False):
    """
    Minimal wrapper with the same signature used in tests.
    Uploads files to the draft's bucket; ignores `publish`.
    Returns an object with `.json()` giving a link payload.
    """
    api_base = _api_base()
    token = access_token or _token()
    dep = _get_deposition(api_base, token, str(deposition_id))
    _upload_files(api_base, token, dep, [Path(p) for p in paths])

    class _Resp:
        def json(self_inner):
            links = dep.get("links", {})
            return {"links": {"html": links.get("latest_draft_html") or links.get("html")}}
    return _Resp()


# ------------------------------
# Management command
# ------------------------------

class Command(BaseCommand):
    help = "Update an existing Zenodo deposition (no publish): patch selected metadata fields and upload latest files."

    def add_arguments(self, parser):
        parser.add_argument("--deposition-id", required=False, help="Zenodo deposition (draft) ID")
        parser.add_argument("--confirm", action="store_true", help="Required to execute (safety switch)")
        parser.add_argument("--patch", default="description,version,keywords,related_identifiers",
                            help="Comma-separated metadata fields to update")
        parser.add_argument("--merge-keywords", action="store_true", help="Union keywords instead of replace")
        parser.add_argument("--merge-related", action="store_true", help="Union related_identifiers instead of replace")
        parser.add_argument("--no-build", action="store_true",
                            help="Do not rebuild README/ZIP/dynamic JSON; use existing files")

    def handle(self, *args, **opts):
        if not opts["confirm"]:
            self.stdout.write("Add --confirm to proceed.")
            return

        deposition_id = opts.get("deposition_id") or getattr(settings, "ZENODO_SANDBOX_DEPOSITION_ID", None)
        if not deposition_id:
            raise SystemExit("No deposition ID provided. Use --deposition-id or settings.ZENODO_SANDBOX_DEPOSITION_ID.")

        project_root = Path(__file__).resolve().parents[3]
        data_dir = project_root / "data"
        data_dir.mkdir(exist_ok=True)

        # Build README/ZIP/dynamic JSON unless skipped
        if not opts.get("no_build"):
            self.stdout.write("Generating optimap-main.zip and README.md…")
            call_command("render_zenodo_desc")

        readme_path = data_dir / "README.md"
        zip_path = data_dir / "optimap-main.zip"
        dyn_path = data_dir / "zenodo_dynamic.json"
        if not readme_path.exists() or not zip_path.exists() or not dyn_path.exists():
            raise SystemExit("Missing artifacts in data/: README.md, optimap-main.zip, zenodo_dynamic.json")

        description_md = readme_path.read_text(encoding="utf-8")
        description_html = _markdown_to_html(description_md)
        dyn = json.loads(dyn_path.read_text(encoding="utf-8"))

        patch_fields = [f.strip() for f in opts["patch"].split(",") if f.strip()]

        api_base = _api_base()
        token = _token()

        dep = _get_deposition(api_base, token, str(deposition_id))
        meta = dep.get("metadata", {}) or {}

        # Selective metadata update (no clobber of unrelated fields)
        incoming: dict = {}
        if "description" in patch_fields:
            incoming["description"] = description_html
        if "version" in patch_fields and "version" in dyn:
            incoming["version"] = dyn["version"]
        if "keywords" in patch_fields and "keywords" in dyn:
            if opts["merge_keywords"]:
                incoming["keywords"] = _merge_list_unique(meta.get("keywords", []), dyn["keywords"])
            else:
                incoming["keywords"] = dyn["keywords"]
        if "related_identifiers" in patch_fields and "related_identifiers" in dyn:
            if opts["merge_related"]:
                incoming["related_identifiers"] = _merge_related(meta.get("related_identifiers", []), dyn["related_identifiers"])
            else:
                incoming["related_identifiers"] = dyn["related_identifiers"]

        new_meta = {**meta, **incoming}

        # Try updating directly; on error (e.g., locked), POST edit then retry
        try:
            _put_metadata(api_base, token, str(deposition_id), new_meta)
            self.stdout.write("Metadata updated (merged, no clobber).")
        except Exception:
            _post_edit(api_base, token, str(deposition_id))
            _put_metadata(api_base, token, str(deposition_id), new_meta)
            self.stdout.write("Metadata updated after edit action.")

        # Ensure dumps exist; regenerate missing ones
        latest = _find_latest_dump_files()
        exts = {(p.suffix if p.suffix != ".gz" else ".geojson.gz") for p in latest}
        try:
            if ".gpkg" not in exts:
                from publications.tasks import regenerate_geopackage_cache
                regenerate_geopackage_cache()
            if (".geojson" not in exts) and (".geojson.gz" not in exts):
                from publications.tasks import regenerate_geojson_cache
                regenerate_geojson_cache()
        except Exception as e:
            self.stderr.write(f"Warning: could not regenerate missing dumps: {e}")
        latest = _find_latest_dump_files()

        self.stdout.write("Uploading files to existing Zenodo sandbox draft…")
        paths = [readme_path, zip_path] + latest

        # IMPORTANT: call the shim POSITIONALLY (no kwargs) for test doubles
        res = update_zenodo(str(deposition_id), paths, ("sandbox." in api_base), token, False)

        html = None
        try:
            html = res.json().get("links", {}).get("html")
        except Exception:
            pass

        if not html:
            dep2 = _get_deposition(api_base, token, str(deposition_id))
            html = dep2.get("links", {}).get("latest_draft_html") or dep2.get("links", {}).get("html")

        self.stdout.write(self.style.SUCCESS(f"✅ Updated deposition {deposition_id} at {html or '(no link)'}"))
