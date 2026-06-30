# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Backfill ``Work.author_links`` by fetching author ORCIDs from OpenAlex and/or OpenAIRE.

Default (``--source both``): tries OpenAlex first for each work, then runs an
OpenAIRE supplement pass that adds any ORCID not yet linked and records rank-
position conflicts in the provenance record without overwriting existing links.
Use ``--source openalex`` or ``--source openaire`` to restrict to one source.

Usage:
    python manage.py backfill_author_orcids
    python manage.py backfill_author_orcids --limit 200
    python manage.py backfill_author_orcids --force          # re-link even if already linked
    python manage.py backfill_author_orcids --dry-run
    python manage.py backfill_author_orcids --source openalex
    python manage.py backfill_author_orcids --source openaire
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db.models import Q

from works.models import Work


class Command(BaseCommand):
    help = "Backfill Work.author_links with author ORCIDs from OpenAlex and/or OpenAIRE."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="Max works to process.")
        parser.add_argument("--force", action="store_true", help="Re-link even if author_links already set.")
        parser.add_argument("--dry-run", action="store_true", help="Report counts without writing.")
        parser.add_argument(
            "--source",
            choices=["openalex", "openaire", "both"],
            default="both",
            help="Enrichment source(s) to use (default: both — OpenAlex first, then OpenAIRE supplement).",
        )
        parser.add_argument(
            "--throttle",
            type=float,
            default=0.1,
            help="Seconds between API requests (default: 0.1).",
        )

    def handle(self, *args, **opts):
        limit = opts["limit"]
        force = opts["force"]
        dry_run = opts["dry_run"]
        source = opts["source"]
        throttle = opts["throttle"]

        qs = Work.objects.filter(Q(status="p") | Q(status="h")).order_by("id")

        if source == "openalex":
            qs = qs.filter(openalex_id__isnull=False).exclude(openalex_id="")
        elif source == "openaire":
            qs = qs.filter(doi__isnull=False).exclude(doi="")
        else:  # both
            qs = qs.filter(Q(openalex_id__isnull=False, openalex_id__gt="") | Q(doi__isnull=False, doi__gt=""))

        if not force:
            qs = qs.filter(author_links__isnull=True)

        if limit:
            qs = qs[:limit]

        total = qs.count()
        self.stdout.write(f"Works to process: {total} (source={source})")
        self.stdout.flush()

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no writes."))
            return

        if source == "openalex":
            self._backfill_from_openalex(qs, force=force, throttle=throttle)
        elif source == "openaire":
            self._backfill_from_openaire(qs, force=force, throttle=throttle)
        else:
            self._backfill_both(qs, force=force, throttle=throttle)

    def _backfill_both(self, qs, *, force, throttle):
        from works.harvesting.openaire import _author_records_from_openaire, fetch_openaire_record
        from works.harvesting.sessions import _openaire_session, _openalex_session
        from works.openalex_matcher import author_records_from_authorships
        from works.services.authors import link_work_authors

        oa_session = _openalex_session()
        aire_session = _openaire_session()
        total = qs.count()
        oa_linked = aire_supplemented = skipped = errors = processed = 0

        for work in qs.iterator(chunk_size=100):
            processed += 1
            work_oa_linked = 0
            parts = []

            # --- OpenAlex pass ---
            if work.openalex_id:
                url = f"https://api.openalex.org/works/{work.openalex_id}"
                try:
                    resp = oa_session.get(url, params={"select": "id,authorships"}, timeout=15)
                    resp.raise_for_status()
                    records = author_records_from_authorships(resp.json().get("authorships") or [])
                    work_oa_linked = link_work_authors(work, records, source="openalex", force=force)
                    parts.append(f"openalex:{work_oa_linked} linked")
                except Exception as exc:
                    self.stderr.write(f"  [{work.id}] OpenAlex error: {exc}")
                    self.stderr.flush()
                    errors += 1
                    parts.append("openalex:error")

            # --- OpenAIRE supplement pass ---
            if work.doi:
                try:
                    record = fetch_openaire_record(work.doi, session=aire_session)
                    if record:
                        records = _author_records_from_openaire(record)
                        n = link_work_authors(work, records, source="openaire", supplement=True)
                        parts.append(f"openaire:+{n}")
                        if n:
                            aire_supplemented += 1
                    else:
                        parts.append("openaire:no record")
                except Exception as exc:
                    self.stderr.write(f"  [{work.id}] OpenAIRE error: {exc}")
                    self.stderr.flush()
                    errors += 1
                    parts.append("openaire:error")

            if work_oa_linked:
                oa_linked += 1
            elif not work.openalex_id and not work.doi:
                skipped += 1

            self.stdout.write(f"  [{processed}/{total}] work {work.id} — {', '.join(parts) or 'skipped'}")
            self.stdout.flush()

            if throttle:
                time.sleep(throttle)

        self.stdout.write(self.style.SUCCESS("\nDone."))
        self.stdout.write(f"  Processed   : {processed}")
        self.stdout.write(self.style.SUCCESS(f"  OA linked   : {oa_linked}"))
        self.stdout.write(self.style.SUCCESS(f"  AIRE suppl. : {aire_supplemented}"))
        self.stdout.write(f"  Skipped     : {skipped}")
        if errors:
            self.stdout.write(self.style.ERROR(f"  Errors      : {errors}"))

    def _backfill_from_openalex(self, qs, *, force, throttle):

        from works.harvesting.sessions import _openalex_session
        from works.openalex_matcher import author_records_from_authorships
        from works.services.authors import link_work_authors

        session = _openalex_session()
        total = qs.count()
        linked = skipped = errors = processed = 0

        for work in qs.iterator(chunk_size=100):
            processed += 1
            openalex_id = work.openalex_id
            url = f"https://api.openalex.org/works/{openalex_id}"
            try:
                resp = session.get(url, params={"select": "id,authorships"}, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                self.stderr.write(f"  [{processed}/{total}] work {work.id} — error: {exc}")
                self.stderr.flush()
                errors += 1
                continue

            authorships = data.get("authorships") or []
            records = author_records_from_authorships(authorships)
            n = link_work_authors(work, records, source="openalex", force=force)
            if n:
                linked += 1
                self.stdout.write(f"  [{processed}/{total}] work {work.id} — {n} linked")
            else:
                skipped += 1
                self.stdout.write(f"  [{processed}/{total}] work {work.id} — skipped")
            self.stdout.flush()

            if throttle:
                time.sleep(throttle)

        self.stdout.write(self.style.SUCCESS("\nDone."))
        self.stdout.write(f"  Processed : {processed}")
        self.stdout.write(self.style.SUCCESS(f"  Linked    : {linked}"))
        self.stdout.write(f"  Skipped   : {skipped}")
        if errors:
            self.stdout.write(self.style.ERROR(f"  Errors    : {errors}"))

    def _backfill_from_openaire(self, qs, *, force, throttle):
        from works.harvesting.openaire import _author_records_from_openaire, fetch_openaire_record
        from works.harvesting.sessions import _openaire_session
        from works.services.authors import link_work_authors

        session = _openaire_session()
        total = qs.count()
        linked = skipped = errors = processed = 0

        for work in qs.iterator(chunk_size=100):
            processed += 1
            if not work.doi:
                skipped += 1
                self.stdout.write(f"  [{processed}/{total}] work {work.id} — no DOI, skipped")
                self.stdout.flush()
                continue
            try:
                record = fetch_openaire_record(work.doi, session=session)
            except Exception as exc:
                self.stderr.write(f"  [{processed}/{total}] work {work.id} — error: {exc}")
                self.stderr.flush()
                errors += 1
                continue

            if not record:
                skipped += 1
                self.stdout.write(f"  [{processed}/{total}] work {work.id} — no OpenAIRE record")
                self.stdout.flush()
                continue

            records = _author_records_from_openaire(record)
            n = link_work_authors(work, records, source="openaire", force=force)
            if n:
                linked += 1
                self.stdout.write(f"  [{processed}/{total}] work {work.id} — {n} linked")
            else:
                skipped += 1
                self.stdout.write(f"  [{processed}/{total}] work {work.id} — skipped")
            self.stdout.flush()

            if throttle:
                time.sleep(throttle)

        self.stdout.write(self.style.SUCCESS("\nDone."))
        self.stdout.write(f"  Processed : {processed}")
        self.stdout.write(self.style.SUCCESS(f"  Linked    : {linked}"))
        self.stdout.write(f"  Skipped   : {skipped}")
        if errors:
            self.stdout.write(self.style.ERROR(f"  Errors    : {errors}"))
