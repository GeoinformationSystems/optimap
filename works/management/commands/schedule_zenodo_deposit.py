# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Schedule the annual Zenodo deposition run.

The deposit cycle (regenerate data dumps → render README/zip/metadata →
update or bootstrap a Zenodo draft) is wrapped in
``works.tasks.run_zenodo_deposition`` and registered with Django-Q as a
yearly schedule. The first run lands on Dec 31 23:59 of the current year
(local time); subsequent runs repeat annually. Publishing the resulting
draft remains manual — admins receive an email with the draft link.

This command is idempotent: re-running it will not add duplicate schedule
entries.
"""

from datetime import datetime

from django.core.management.base import BaseCommand
from django_q.models import Schedule
from django_q.tasks import schedule


FUNC_NAME = "works.tasks.run_zenodo_deposition"


class Command(BaseCommand):
    help = (
        "Schedule the annual Zenodo deposition run (Dec 31 23:59, yearly). "
        "Idempotent."
    )

    def handle(self, *args, **options):
        if Schedule.objects.filter(func=FUNC_NAME).exists():
            self.stdout.write("Zenodo deposition is already scheduled.")
            return

        now = datetime.now()
        next_run = now.replace(
            month=12, day=31, hour=23, minute=59, second=0, microsecond=0
        )
        if next_run <= now:
            next_run = next_run.replace(year=now.year + 1)

        schedule(
            FUNC_NAME,
            schedule_type=Schedule.YEARLY,
            repeats=-1,
            next_run=next_run,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Scheduled annual Zenodo deposition for {next_run.isoformat()}."
            )
        )
