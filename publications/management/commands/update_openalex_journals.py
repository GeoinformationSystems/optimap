# publications/management/commands/update_openalex_journals.py

from django.core.management.base import BaseCommand
from publications.models import Journal
import requests

def fetch_openalex_for_issn(issn: str) -> dict | None:
    """
    Query OpenAlex for a given ISSN-L and return the JSON dict.
    Follows 302 redirects if necessary.
    """
    try:
        # Initial request to /sources/issn:<ISSN>
        resp = requests.get(f"https://api.openalex.org/sources/issn:{issn}", timeout=10)
        # If OpenAlex returns a 302 redirect, follow it to the canonical URL
        if resp.status_code == 302 and "Location" in resp.headers:
            resp = requests.get(resp.headers["Location"], timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None

class Command(BaseCommand):
    help = "Update Journal metadata (openalex_id, publisher_name, works_count, works_api_url, etc.) from OpenAlex."

    def handle(self, *args, **options):
        journals_qs = Journal.objects.exclude(issn_l__isnull=True)
        total = journals_qs.count()
        self.stdout.write(f"Found {total} journal(s) with ISSN-L.")

        for journal in journals_qs:
            data = fetch_openalex_for_issn(journal.issn_l)
            if not data:
                self.stdout.write(f"Skipped (no data): {journal.name}")
                continue

            changed = False

            # 1. openalex_id & openalex_url
            new_openalex = data.get("id")  # e.g., "https://openalex.org/S137773608"
            if new_openalex and journal.openalex_id != new_openalex:
                journal.openalex_id = new_openalex
                journal.openalex_url = new_openalex  # mirror the same URL
                changed = True

            # 2. works_count & works_api_url
            new_works_count = data.get("works_count")
            if new_works_count is not None and journal.works_count != new_works_count:
                journal.works_count = new_works_count
                changed = True

            api_url = data.get("works_api_url")
            if api_url and journal.works_api_url != api_url:
                journal.works_api_url = api_url
                changed = True

            # 3. publisher_name: read from "host_organization.display_name"
            host_org = data.get("host_organization", {})
            new_publisher = None
            if isinstance(host_org, dict):
                new_publisher = host_org.get("display_name")
            # Fallback: if still None, use data["display_name"] as proxy
            if not new_publisher:
                new_publisher = data.get("display_name")
            if new_publisher and journal.publisher_name != new_publisher:
                journal.publisher_name = new_publisher
                changed = True

            if changed:
                journal.save()
                self.stdout.write(f"Updated: {journal.name} ({journal.issn_l})")
            else:
                self.stdout.write(f"Skipped (unchanged): {journal.name}")

        self.stdout.write("Done updating OpenAlex metadata.")
