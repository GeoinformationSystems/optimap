# publications/management/commands/render_zenodo_desc.py
import json
import subprocess
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from django.conf import settings
from django.core.management.base import BaseCommand
from jinja2 import Environment, FileSystemLoader

from publications.models import Publication, Source


def _canon_url(raw: str | None, *, force_https_for_optimap: bool = False) -> str | None:
    """
    Canonicalize source URLs.
    - If URL has no scheme, default to http (README tests expect http for optimap.science).
    - If force_https_for_optimap=True and host ends with optimap.science, use https.
    - Normalize cases with only a host in 'path'.
    """
    if not raw:
        return None
    p = urlparse(raw)

    # Handle host stored in path with no scheme/netloc (e.g., "example.org")
    if not p.scheme and p.path and not p.netloc and "." in p.path:
        p = p._replace(netloc=p.path, path="")

    host = (p.hostname or "").lower()
    scheme = p.scheme or "http"  # README: keep http by default

    if force_https_for_optimap and host.endswith("optimap.science"):
        scheme = "https"

    return urlunparse((scheme, p.netloc or "", p.path or "", "", "", ""))


def _label_from(name: str | None, url: str) -> str:
    """
    Human-friendly label; fix numeric-only names (e.g., '2000') and optimap.
    """
    n = (name or "").strip()
    if not n or n.isdigit():
        host = (urlparse(url).hostname or "").lower()
        if host == "optimap.science":
            return "OPTIMAP"
        core = host.removeprefix("www.")
        if "." in core:
            core = core.split(".")[0]
        return core.capitalize() or url
    return n


class Command(BaseCommand):
    help = "Generate optimap-main.zip, README.md, and zenodo_dynamic.json into the project's data/ folder"

    def handle(self, *args, **options):
        project_root = Path(__file__).resolve().parents[3]
        data_dir = project_root / "data"
        data_dir.mkdir(exist_ok=True)

        # 1) Archive current HEAD
        archive_path = data_dir / "optimap-main.zip"
        self.stdout.write(f"Archiving current branch to {archive_path}")
        subprocess.run(
            ["git", "archive", "--format=zip", "HEAD", "-o", str(archive_path)],
            cwd=str(project_root),
            check=True,
        )

        # 2) Template env
        tmpl_dir = project_root / "publications" / "templates"
        env = Environment(
            loader=FileSystemLoader(str(tmpl_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template("README.md.j2")

        # 3) Stats
        article_count = Publication.objects.count()
        spatial_count = Publication.objects.exclude(geometry=None).count()
        temporal_count = Publication.objects.exclude(timeperiod_startdate=None).count()
        earliest_date = (
            Publication.objects.order_by("publicationDate")
            .values_list("publicationDate", flat=True)
            .first()
            or ""
        )
        latest_date = (
            Publication.objects.order_by("-publicationDate")
            .values_list("publicationDate", flat=True)
            .first()
            or ""
        )

        # 4) Sources for README (preserve original scheme; default http). De-duplicate by hostname.
        sources: list[dict] = []
        seen_hosts: set[str] = set()
        for src in Source.objects.all().order_by("id"):
            raw_url = getattr(src, "homepage", None) or getattr(src, "url_field", None) or getattr(src, "url", None)
            url_for_readme = _canon_url(raw_url, force_https_for_optimap=False)
            if not url_for_readme:
                continue

            host = (urlparse(url_for_readme).hostname or "").lower().removeprefix("www.")
            if host in seen_hosts:
                continue
            seen_hosts.add(host)

            label = _label_from(getattr(src, "name", None), url_for_readme)
            sources.append({"name": label, "url": url_for_readme})

        # 5) Version bump (vN)
        version_file = data_dir / "last_version.txt"
        if version_file.exists():
            try:
                last = int((version_file.read_text().strip() or "v0").lstrip("v"))
            except Exception:
                last = 0
        else:
            last = 0
        version = f"v{last + 1}"
        version_file.write_text(version, encoding="utf-8")

        # 6) Dynamic JSON for Zenodo deploy
        base_url = getattr(settings, "BASE_URL", "https://optimap.science").rstrip("/")
        related_identifiers = [
            {
                "relation": "isSupplementTo",
                "identifier": f"{base_url}/data/optimap_data_dump_latest.geojson.gz",
                "scheme": "url",
            },
            {
                "relation": "isSupplementTo",
                "identifier": f"{base_url}/data/optimap_data_dump_latest.gpkg",
                "scheme": "url",
            },
        ]
        seen_ids = {ri["identifier"] for ri in related_identifiers}
        for s in sources:
            https_url = _canon_url(s["url"], force_https_for_optimap=True)
            if https_url and https_url not in seen_ids:
                related_identifiers.append(
                    {"relation": "describes", "identifier": https_url, "scheme": "url"}
                )
                seen_ids.add(https_url)

        dyn = {
            "version": version,
            "keywords": ["Open Access", "Open Science", "ORI", "Open Data", "FAIR"],
            "related_identifiers": related_identifiers,
        }
        (data_dir / "zenodo_dynamic.json").write_text(json.dumps(dyn, indent=2), encoding="utf-8")

        # 7) Render README (no funding in README per supervisor)
        rendered = template.render(
            version=version,
            date=date.today().isoformat(),
            article_count=article_count,
            sources=sources,
            spatial_count=spatial_count,
            temporal_count=temporal_count,
            earliest_date=earliest_date,
            latest_date=latest_date,
            funders=[],
        )
        (data_dir / "README.md").write_text(rendered, encoding="utf-8")

        # 8) Done
        self.stdout.write(
            self.style.SUCCESS(
                f"Generated assets in {data_dir}:\n"
                f" - {archive_path.name}\n"
                f" - README.md\n"
                f" - zenodo_dynamic.json"
            )
        )
