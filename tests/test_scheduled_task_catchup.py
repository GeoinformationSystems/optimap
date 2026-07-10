# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for scheduled-task catch-up behaviour.

Covers:
- the ``log_scheduled_catchup`` decorator (logs late scheduled runs, never
  skips, transparent to manual/ad-hoc calls), and
- that recurring schedules carry ``intended_date_kwarg="scheduled_for"`` while
  manual one-off (ONCE) schedules do not, so manual runs are never treated as
  scheduled catch-ups.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from django_q.models import Schedule

from works.apps import schedule_data_dump
from works.models import Source
from works.utils.scheduling import log_scheduled_catchup

LOGGER_NAME = "works.utils.scheduling"


@override_settings(SCHEDULED_TASK_CATCHUP_THRESHOLD_MINUTES=5)
class LogScheduledCatchupTest(TestCase):
    def _spy(self):
        calls = []

        @log_scheduled_catchup
        def task(source_id, flag=False):
            calls.append((source_id, flag))
            return f"ran-{source_id}"

        return task, calls

    def test_on_time_scheduled_run_executes_without_warning(self):
        task, calls = self._spy()
        with self.assertNoLogs(LOGGER_NAME, level="WARNING"):
            result = task(7, scheduled_for=timezone.now().isoformat())
        self.assertEqual(calls, [(7, False)])
        self.assertEqual(result, "ran-7")

    def test_late_scheduled_run_logs_catchup_warning(self):
        task, calls = self._spy()
        late = (timezone.now() - timedelta(minutes=6)).isoformat()
        with self.assertLogs(LOGGER_NAME, level="WARNING") as cm:
            result = task(7, scheduled_for=late)
        self.assertEqual(calls, [(7, False)])  # still runs — never skipped
        self.assertEqual(result, "ran-7")
        self.assertIn("missed runs were skipped", "\n".join(cm.output))
        self.assertIn("task", "\n".join(cm.output))

    def test_manual_run_without_scheduled_for_never_logs(self):
        task, calls = self._spy()
        with self.assertNoLogs(LOGGER_NAME, level="WARNING"):
            result = task(7, flag=True)
        self.assertEqual(calls, [(7, True)])
        self.assertEqual(result, "ran-7")

    def test_scheduled_for_is_popped_before_call(self):
        # A strict-signature function (no **kwargs) must not receive scheduled_for.
        ran = []

        @log_scheduled_catchup
        def strict(source_id):
            ran.append(source_id)

        # Would raise TypeError if scheduled_for leaked through.
        strict(42, scheduled_for=timezone.now().isoformat())
        self.assertEqual(ran, [42])

    def test_exception_propagates(self):
        @log_scheduled_catchup
        def boom():
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError):
            boom(scheduled_for=timezone.now().isoformat())

    def test_unparseable_scheduled_for_does_not_break_run(self):
        task, calls = self._spy()
        with self.assertNoLogs(LOGGER_NAME, level="WARNING"):
            result = task(7, scheduled_for="not-a-date")
        self.assertEqual(calls, [(7, False)])
        self.assertEqual(result, "ran-7")

    @override_settings(SCHEDULED_TASK_CATCHUP_THRESHOLD_MINUTES=60)
    def test_threshold_is_configurable(self):
        task, calls = self._spy()
        # 6 minutes late is under the 60-minute threshold → no warning.
        late = (timezone.now() - timedelta(minutes=6)).isoformat()
        with self.assertNoLogs(LOGGER_NAME, level="WARNING"):
            task(7, scheduled_for=late)
        self.assertEqual(calls, [(7, False)])


def _make_source(name, interval=120):
    return Source.objects.create(
        name=name,
        url_field=f"http://example.org/{name}",
        harvest_interval_minutes=interval,
    )


class ScheduleIntendedDateKwargTest(TestCase):
    def test_cluster_catch_up_is_disabled(self):
        self.assertIs(settings.Q_CLUSTER.get("catch_up"), False)

    def test_source_save_sets_intended_date_kwarg(self):
        source = _make_source("WithKwarg")
        sched = Schedule.objects.get(name=f"Harvest Source {source.id}")
        self.assertEqual(sched.intended_date_kwarg, "scheduled_for")

    def test_reset_harvest_schedules_sets_intended_date_kwarg(self):
        source = _make_source("ResetKwarg")
        call_command("reset_harvest_schedules", "--no-stagger", stdout=StringIO())
        sched = Schedule.objects.get(name=f"Harvest Source {source.id}")
        self.assertEqual(sched.intended_date_kwarg, "scheduled_for")

    def test_data_dump_schedule_sets_intended_date_kwarg(self):
        Schedule.objects.filter(func="works.tasks.regenerate_all_data_dumps").delete()
        schedule_data_dump(sender=None)
        sched = Schedule.objects.get(func="works.tasks.regenerate_all_data_dumps")
        self.assertEqual(sched.intended_date_kwarg, "scheduled_for")

    def test_inactivity_schedules_set_intended_date_kwarg(self):
        from works.tasks import schedule_inactivity_deletion_task, schedule_inactivity_warning_task

        Schedule.objects.filter(func__startswith="works.tasks.send_inactivity").delete()
        schedule_inactivity_warning_task()
        schedule_inactivity_deletion_task()
        for func in (
            "works.tasks.send_inactivity_warning_emails",
            "works.tasks.send_inactivity_deletion_list_to_admins",
        ):
            sched = Schedule.objects.get(func=func)
            self.assertEqual(sched.intended_date_kwarg, "scheduled_for", func)

    def test_email_schedules_use_works_dotted_path(self):
        from works.tasks import (
            schedule_monthly_email_task,
            schedule_subscription_email_task,
            schedule_weekly_subscription_email_task,
        )

        schedule_monthly_email_task()
        schedule_subscription_email_task()
        schedule_weekly_subscription_email_task()
        self.assertFalse(Schedule.objects.filter(func__startswith="publications.tasks.").exists())
        monthly = Schedule.objects.get(func="works.tasks.send_monthly_email")
        self.assertEqual(monthly.intended_date_kwarg, "scheduled_for")
        self.assertEqual(
            Schedule.objects.filter(func="works.tasks.send_subscription_based_email").count(),
            2,
            "expected both monthly and weekly subscription schedules",
        )

    def test_manual_once_schedule_has_no_intended_date_kwarg(self):
        from works.admin import schedule_harvesting

        source = _make_source("ManualOnce")
        schedule_harvesting(MagicMock(), MagicMock(), Source.objects.filter(pk=source.pk))
        manual = Schedule.objects.get(name=f"Manual Harvest Source {source.id}")
        self.assertFalse(manual.intended_date_kwarg)  # None or "" → never treated as a scheduled catch-up
        self.assertEqual(manual.schedule_type, Schedule.ONCE)

    def test_backfill_schedules_set_intended_date_kwarg(self):
        from works.tasks import (
            schedule_backfill_author_orcids,
            schedule_backfill_work_countries,
            schedule_backfill_work_regions,
        )

        Schedule.objects.filter(
            func__in=(
                "works.tasks.backfill_work_countries",
                "works.tasks.backfill_work_regions",
                "works.tasks.backfill_author_orcids_task",
            )
        ).delete()
        schedule_backfill_work_countries()
        schedule_backfill_work_regions()
        schedule_backfill_author_orcids()
        for func in (
            "works.tasks.backfill_work_countries",
            "works.tasks.backfill_work_regions",
            "works.tasks.backfill_author_orcids_task",
        ):
            sched = Schedule.objects.get(func=func)
            self.assertEqual(sched.intended_date_kwarg, "scheduled_for", func)

    def test_backfill_targets_accept_scheduled_for_without_raising(self):
        # Regression: backfill_work_countries/backfill_work_regions were
        # scheduled with intended_date_kwarg="scheduled_for" (so Django-Q's
        # scheduler injects that kwarg on every run) but were missing the
        # @log_scheduled_catchup decorator that pops it — every *scheduled*
        # (not manual/direct) invocation raised TypeError. Reproduced by
        # calling exactly as the scheduler does: with a scheduled_for kwarg.
        from works.tasks import backfill_work_countries, backfill_work_regions

        backfill_work_countries(scheduled_for=timezone.now().isoformat())  # must not raise
        backfill_work_regions(scheduled_for=timezone.now().isoformat())  # must not raise
