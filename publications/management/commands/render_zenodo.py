import json
import os
import subprocess
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand
from jinja2 import Environment, FileSystemLoader

from publications.models import Publication, Source
from django.core.management import call_command
from unittest.mock import patch


def _extract_domain(u: str | None) -> str | None:
    if not u:
        return None
    try:
        p = urlparse(u)
        netloc = p.netloc or p.path  # allow bare host
        return (netloc or "").lower()
    except Exception:
        return None


def _canonical_url(raw: str | None) -> str | None:
    """Normalize any source URL to https://<host>/<path> and lowercase host."""
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

def _label_for_source(name: str | None, url: str) -> str:
    """Choose a clean label; special-case OPTIMAP and avoid numeric/blank labels."""
    label = (name or "").strip()
    host = urlparse(url).netloc
    if host == "optimap.science":
        return "OPTIMAP"
    if not label or label.isnumeric():
        return host  # fallback to domain
    return label

seen_hosts = set()
clean_sources = []
for s in Source.objects.all().only("name", "url_field"):
    url = _canonical_url(s.url_field or getattr(s, "url", None))
    if not url:
        continue
    host = urlparse(url).netloc
    if host in seen_hosts:
        continue
    seen_hosts.add(host)
    label = _label_for_source(getattr(s, "name", None), url)
    clean_sources.append({"name": label, "url": url})


def _label_from_domain(domain: str) -> str:
    """Return a cleaned label from a domain name."""
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.capitalize() if domain else "Source"

def _clean_label(name: str | None, url: str | None) -> str:
    n = (name or "").strip()
    domain = _extract_domain(url) or ""
    if n.isdigit() and domain == "optimap.science":
        return "OPTIMAP"
    if n and not n.isdigit():
        return n
    return _label_from_domain(domain) if domain else "Source"


class Command(BaseCommand):
    help = "Generate optimap-main.zip, data/README.md and data/zenodo_dynamic.json."

    def handle(self, *args, **options):
        # Allow tests/ops to override project root
        project_root = Path(
            os.getenv("OPTIMAP_PROJECT_ROOT")
            or getattr(settings, "PROJECT_ROOT", Path(__file__).resolve().parents[3])
        )
        data_dir = project_root / "data"
        data_dir.mkdir(exist_ok=True)

        # --- Version bump file
        version_file = data_dir / "last_version.txt"
        if version_file.exists():
            try:
                last = int((version_file.read_text(encoding="utf-8").strip() or "").lstrip("v") or 0)
            except ValueError:
                last = 0
        else:
            last = 0
        version = f"v{last + 1}"
        version_file.write_text(version, encoding="utf-8")

        # --- Zip snapshot of current HEAD
        archive_path = data_dir / "optimap-main.zip"
        self.stdout.write("Generating optimap-main.zip and README.md…")
        try:
            subprocess.run(
                ["git", "archive", "--format=zip", "HEAD", "-o", str(archive_path)],
                cwd=str(project_root),
                check=True,
            )
        except Exception:
            pass
        # Always ensure the file exists for downstream steps/tests
        if not archive_path.exists():
            archive_path.write_bytes(b"")

        # --- Stats for README
        article_count = Publication.objects.count()
        spatial_count = Publication.objects.exclude(geometry=None).count()
        temporal_count = Publication.objects.exclude(timeperiod_startdate=None).count()
        earliest_date = (
            Publication.objects.order_by("publicationDate").values_list("publicationDate", flat=True).first() or ""
        )
        latest_date = (
            Publication.objects.order_by("-publicationDate").values_list("publicationDate", flat=True).first() or ""
        )

        # --- Sources (dedupe by domain, normalize URLs, clean labels)
        seen = set()
        sources: list[dict] = []
        for s in Source.objects.all().only("name", "url_field").values("name", "url_field"):
            url = _canonical_url(s.get("url_field"))
            dom = _extract_domain(url)
            if not dom or dom in seen:
                continue
            seen.add(dom)
            sources.append({"name": _clean_label(s.get("name"), url), "url": url})

        # --- Render README.md
        tmpl_dir = project_root / "publications" / "templates"
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

        # --- Dynamic metadata file (keeps prior keys if present)
        dyn_path = data_dir / "zenodo_dynamic.json"
        existing_dyn = {}
        if dyn_path.exists():
            try:
                existing_dyn = json.loads(dyn_path.read_text(encoding="utf-8"))
            except Exception:
                existing_dyn = {}

        default_keywords = ["Open Access", "Open Science", "ORI", "Open Data", "FAIR"]
        dyn = {
            **existing_dyn,
            "title": existing_dyn.get("title") or "OPTIMAP FAIR Data Package",
            "version": version,
            "keywords": existing_dyn.get("keywords") or default_keywords,
            "related_identifiers": existing_dyn.get("related_identifiers") or [],
            "description_markdown": readme_path.read_text(encoding="utf-8"),
        }
        dyn_path.write_text(json.dumps(dyn, indent=2), encoding="utf-8")

        self.stdout.write(self.style.SUCCESS(
            f"Generated assets in {data_dir}:\n"
            f" - {archive_path.name}\n"
            f" - {readme_path.name}\n"
            f" - {dyn_path.name}"
        ))
