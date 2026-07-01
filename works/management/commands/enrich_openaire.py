# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Backfill missing abstract / keywords / authors on existing works from OpenAIRE.

The live harvest pipeline enriches new works automatically (see
``complete_harvest`` → ``enrich_event_from_openaire``); this command covers
works harvested before OpenAIRE enrichment existed, or any subset selected by
collection / DOI prefix / source.

Usage:
    python manage.py enrich_openaire                         # all works missing a target field
    python manage.py enrich_openaire --collection agile-gi   # AGILE GI works only
    python manage.py enrich_openaire --doi-prefix 10.1007/978- --limit 20
    python manage.py enrich_openaire --throttle 1            # with a token set (7200/hour)
    python manage.py enrich_openaire --dry-run               # query OpenAIRE, write nothing
    python manage.py enrich_openaire --collection eartharxiv --async  # background (needs qcluster)

OpenAIRE is rate-limited to 60 requests/hour anonymously; set
OPTIMAP_OPENAIRE_TOKEN and lower --throttle for larger backfills.
"""

import logging

from django.core.management.base import BaseCommand
from django_q.humanhash import humanize as humanize_task_id
from django_q.tasks import async_task

from works.harvesting.openaire import openaire_task_q_options
from works.tasks import enrich_openaire_backfill

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Enrich works that lack an abstract/keywords/authors with metadata from OpenAIRE"

    def add_arguments(self, parser):
        parser.add_argument(
            "--collection",
            default=None,
            help="Limit to works in this collection identifier (e.g. agile-gis).",
        )
        parser.add_argument(
            "--doi-prefix",
            default=None,
            help="Limit to works whose DOI starts with this prefix (e.g. '10.1007/978-').",
        )
        parser.add_argument(
            "--source",
            default=None,
            help="Limit to works from this Source (numeric id or exact name).",
        )
        parser.add_argument("--limit", type=int, default=None, help="Maximum number of works to process.")
        parser.add_argument(
            "--throttle",
            type=float,
            default=None,
            metavar="SECONDS",
            help="Seconds between OpenAIRE requests (default: OPTIMAP_OPENAIRE_ENRICH_THROTTLE).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Query OpenAIRE even for works that already have all target fields.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Query OpenAIRE and report, but do not write to the database.",
        )
        parser.add_argument(
            "--async",
            dest="async_mode",
            action="store_true",
            help=(
                "Enqueue the backfill as a single Django-Q task instead of "
                "running it synchronously. Requires a running qcluster (python "
                "manage.py qcluster); without one the task sits in the broker "
                "queue and never executes. Prints the enqueued task id and its "
                "humanized admin name (searchable under Django Q → Successful/"
                "Failed tasks) and returns immediately — watch the Q worker log "
                "/ task result for the summary."
            ),
        )

    def handle(self, *args, **options):
        filters = {
            "collection": options["collection"],
            "doi_prefix": options["doi_prefix"],
            "source": options["source"],
            "limit": options["limit"],
            "throttle": options["throttle"],
            "force": options["force"],
            "dry_run": options["dry_run"],
        }

        if options["async_mode"]:
            task_id = async_task(
                "works.tasks.enrich_openaire_backfill",
                q_options=openaire_task_q_options(),
                chunked=True,
                **filters,
            )
            # Django-Q stores the task under a humanized form of the UUID; that is
            # the value shown/searchable in the admin "Name" column (Successful /
            # Failed tasks). The raw UUID only appears on the Queued tasks page.
            task_name = humanize_task_id(task_id)
            self.stdout.write(
                self.style.SUCCESS(f"Enqueued OpenAIRE backfill as Django-Q task '{task_name}' (id: {task_id}).")
            )
            self.stdout.write(
                "Requires a running qcluster (python manage.py qcluster). Find it in the "
                "Django admin under Django Q → Queued tasks (by id while it waits), then "
                f"Successful/Failed tasks (search Name for '{task_name}'). Progress and "
                "the summary go to the Q worker log / task result."
            )
            return

        summary = enrich_openaire_backfill(**filters, on_progress=self.stdout.write)

        self.stdout.write(self.style.SUCCESS("\nDone."))
        self.stdout.write(f"  Processed : {summary['processed']}")
        self.stdout.write(self.style.SUCCESS(f"  Updated   : {summary['updated']}"))
        self.stdout.write(f"  No match  : {summary['no_match']}")
        if summary["failed"]:
            self.stdout.write(self.style.ERROR(f"  Failed    : {summary['failed']}"))
        if filters["dry_run"]:
            self.stdout.write(self.style.WARNING("(DRY RUN — no changes saved)"))
