# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Backfill ``Work.author_links`` by fetching author ORCIDs from OpenAlex and/or OpenAIRE.

Default (``--source both``): tries OpenAlex first for each work, then runs an
OpenAIRE supplement pass that adds any ORCID not yet linked and records rank-
position conflicts in the provenance record without overwriting existing links.
Use ``--source openalex`` or ``--source openaire`` to restrict to one source.

Thin wrapper over ``works.tasks.backfill_author_orcids_task``: runs synchronously
by default (interruptible with Ctrl-C), or enqueues a single self-re-enqueuing
Django-Q chunk with ``--async`` (requires a running qcluster).

Usage:
    python manage.py backfill_author_orcids
    python manage.py backfill_author_orcids --limit 200
    python manage.py backfill_author_orcids --force          # re-link even if already linked
    python manage.py backfill_author_orcids --dry-run
    python manage.py backfill_author_orcids --source openalex
    python manage.py backfill_author_orcids --source openaire
    python manage.py backfill_author_orcids --async          # background (needs qcluster)
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django_q.humanhash import humanize as humanize_task_id
from django_q.tasks import async_task

from works.harvesting.openaire import openaire_task_q_options
from works.tasks import backfill_author_orcids_task


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
            default=None,
            metavar="SECONDS",
            help="Seconds between API requests (default: OPTIMAP_ORCID_BACKFILL_THROTTLE).",
        )
        parser.add_argument(
            "--async",
            dest="async_mode",
            action="store_true",
            help=(
                "Enqueue the backfill as a self-re-enqueuing Django-Q task instead "
                "of running it synchronously. Requires a running qcluster (python "
                "manage.py qcluster); without one the task sits in the broker queue "
                "and never executes. Prints the enqueued task id and its humanized "
                "admin name (searchable under Django Q → Successful/Failed tasks) "
                "and returns immediately — watch the Q worker log / task result for "
                "the summary."
            ),
        )

    def handle(self, *args, **opts):
        filters = {
            "source": opts["source"],
            "limit": opts["limit"],
            "force": opts["force"],
            "dry_run": opts["dry_run"],
            "throttle": opts["throttle"],
        }

        if opts["async_mode"]:
            task_id = async_task(
                "works.tasks.backfill_author_orcids_task",
                q_options=openaire_task_q_options(),
                chunked=True,
                **filters,
            )
            # Django-Q stores the task under a humanized form of the UUID; that is
            # the value shown/searchable in the admin "Name" column (Successful /
            # Failed tasks). The raw UUID only appears on the Queued tasks page.
            task_name = humanize_task_id(task_id)
            self.stdout.write(
                self.style.SUCCESS(f"Enqueued author ORCID backfill as Django-Q task '{task_name}' (id: {task_id}).")
            )
            self.stdout.write(
                "Requires a running qcluster (python manage.py qcluster). Find it in the "
                "Django admin under Django Q → Queued tasks (by id while it waits), then "
                f"Successful/Failed tasks (search Name for '{task_name}'). Progress and "
                "the summary go to the Q worker log / task result."
            )
            return

        summary = backfill_author_orcids_task(**filters, on_progress=self.stdout.write)

        self.stdout.write(self.style.SUCCESS("\nDone."))
        self.stdout.write(f"  Processed    : {summary['processed']}")
        self.stdout.write(self.style.SUCCESS(f"  Linked       : {summary['linked']}"))
        if opts["source"] == "both":
            self.stdout.write(self.style.SUCCESS(f"  Supplemented : {summary['supplemented']}"))
        self.stdout.write(f"  Skipped      : {summary['skipped']}")
        if summary["errors"]:
            self.stdout.write(self.style.ERROR(f"  Errors       : {summary['errors']}"))
        if summary["dry_run"]:
            self.stdout.write(self.style.WARNING("(DRY RUN — no changes saved)"))
