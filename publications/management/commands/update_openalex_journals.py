# publications/management/commands/update_openalex_journals.py

from django.core.management.base import BaseCommand
from django.db.models import Q
from publications.models import Source
import requests

ISSN_ENDPOINT   = "https://api.openalex.org/sources/issn:{issn}"
SEARCH_ENDPOINT = "https://api.openalex.org/sources"

def fetch_by_issn(issn: str) -> dict | None:
    try:
        resp = requests.get(ISSN_ENDPOINT.format(issn=issn), timeout=10)
        if resp.status_code == 302 and "Location" in resp.headers:
            resp = requests.get(resp.headers["Location"], timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None

def fetch_by_name(name: str) -> dict | None:
    try:
        params = {"filter": f"display_name.search:{name}"}
        resp = requests.get(SEARCH_ENDPOINT, params=params, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None
    except requests.RequestException:
        pass
    return None

class Command(BaseCommand):
    help = (
        "Update Source metadata from OpenAlex. "
        "Works for ISSN-based journals and name-based preprints."
    )

    def handle(self, *args, **options):
        qs = Source.objects.filter(Q(issn_l__isnull=False) | Q(is_preprint=True))
        self.stdout.write(f"Found {qs.count()} source(s) to update.\n")

        for src in qs:
            lookup_key = src.issn_l or src.name
            self.stdout.write(f"[{lookup_key}] querying OpenAlex…")

            if src.issn_l:
                data = fetch_by_issn(src.issn_l)
            else:
                data = fetch_by_name(src.name)

            if not data:
                self.stderr.write(f"[{lookup_key}] skipped (no data)\n")
                continue

            host = data.get("host_organization") or {}
            if not isinstance(host, dict):
                host = {}

            changed = False

            def safe_upd(field: str, new, fmt="{}"):
                nonlocal changed
                if not hasattr(src, field):
                    return
                old = getattr(src, field)
                # treat empty strings or None as "no update"
                if new is None or (isinstance(new, str) and not new.strip()):
                    return
                if new != old:
                    setattr(src, field, new)
                    self.stdout.write(f"  • {field}: {old!r} → {new!r}")
                    changed = True

            # 1. OpenAlex core identifiers & counts
            safe_upd("openalex_id", data.get("id"))
            safe_upd("openalex_url", data.get("id"))
            safe_upd("works_count", data.get("works_count"))

            # 2. Titles & URLs
            safe_upd("abbreviated_title", data.get("abbreviated_title"))
            safe_upd("homepage_url", data.get("homepage_url"))

            # 3. Publisher/display name
            publisher = host.get("display_name") or data.get("display_name")
            safe_upd("publisher_name", publisher)

            # 4. Statistics & flags
            safe_upd("cited_by_count", data.get("cited_by_count"))
            safe_upd("apc_usd", data.get("apc_usd"))
            safe_upd("country_code", data.get("country_code"))
            safe_upd("is_oa", data.get("is_oa", False))
            safe_upd("is_core", data.get("is_core", False))
            safe_upd("is_in_doaj", data.get("is_in_doaj", False))
            safe_upd("type", data.get("type"))
            safe_upd("updated_date", data.get("updated_date"))

            if changed:
                src.save()
                self.stdout.write(f"[{lookup_key}] saved\n")
            else:
                self.stdout.write(f"[{lookup_key}] nothing changed\n")

        self.stdout.write("Done updating OpenAlex metadata.")
