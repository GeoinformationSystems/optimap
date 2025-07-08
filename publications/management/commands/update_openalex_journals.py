# publications/management/commands/update_openalex_journals.py

import logging
import requests

from django.core.management.base import BaseCommand
from django.db.models import Q
from publications.models import Source

logger = logging.getLogger(__name__)

ISSN_ENDPOINT   = "https://api.openalex.org/sources/issn:{issn}"
SEARCH_ENDPOINT = "https://api.openalex.org/sources"

def fetch_by_issn(issn: str) -> dict | None:
    try:
        resp = requests.get(ISSN_ENDPOINT.format(issn=issn), timeout=10)
        # follow manual 302 if returned
        if resp.status_code == 302 and "Location" in resp.headers:
            resp = requests.get(resp.headers["Location"], timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException as e:
        logger.debug("ISSN lookup failed for %s: %s", issn, e)
    return None

def fetch_by_name(name: str) -> dict | None:
    try:
        resp = requests.get(
            SEARCH_ENDPOINT,
            params={"filter": f"display_name.search:{name}"},
            timeout=10
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None
    except requests.RequestException as e:
        logger.debug("Name lookup failed for %s: %s", name, e)
    return None

class Command(BaseCommand):
    help = "Update Source metadata from OpenAlex (ISSN-based or name lookup)."

    def handle(self, *args, **options):
        qs = Source.objects.filter(Q(issn_l__isnull=False) | Q(is_preprint=True))
        total = qs.count()
        self.stdout.write(f"Found {total} source(s) to update.\n")

        for src in qs:
            try:
                key = src.issn_l or src.name
                self.stdout.write(f"[{key}] querying OpenAlex…")
                logger.info("Fetching source metadata for %s", key)

                # fetch metadata (by ISSN or by name)
                data = (
                    fetch_by_issn(src.issn_l)
                    if src.issn_l
                    else fetch_by_name(src.name)
                )
                if not data:
                    logger.info("Skipped %s: no OpenAlex data", key)
                    self.stdout.write(f"[{key}] nothing found\n")
                    continue

                changed = False
                def safe_upd(field: str, new):
                    nonlocal changed
                    old = getattr(src, field, None)
                    if new and new != old:
                        logger.info("%s: %s changed %r → %r", key, field, old, new)
                        setattr(src, field, new)
                        changed = True

                # extract full URI and short ID
                full_id = data.get("id")
                openalex_id = full_id.rsplit("/", 1)[-1] if isinstance(full_id, str) else None

                # update fields
                safe_upd("openalex_id",   openalex_id)
                safe_upd("openalex_url",  full_id)
                safe_upd("works_count",   data.get("works_count"))
                safe_upd("works_api_url", data.get("works_api_url"))

                # compute publisher name safely
                raw_host = data.get("host_organization")
                if isinstance(raw_host, dict):
                    publisher = raw_host.get("display_name") or data.get("display_name")
                else:
                    publisher = data.get("display_name")
                safe_upd("publisher_name", publisher)

                # save if any field changed
                if changed:
                    src.save()
                    self.stdout.write(f"[{key}] saved\n")
                else:
                    self.stdout.write(f"[{key}] nothing changed\n")

            except Exception as e:
                logger.error("Error updating %s: %s", key, e, exc_info=True)
                self.stdout.write(f"[{key}] skipped due to error: {e}\n")
                continue

        self.stdout.write("Done updating OpenAlex metadata.")
