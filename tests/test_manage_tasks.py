# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for /manage/tasks/ and works/qmonitor.py (issue #130)."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from django_q.models import Schedule, Task

from works import qmonitor

User = get_user_model()


def _make_task(**overrides):
    now = timezone.now()
    defaults = dict(started=now, stopped=now)
    defaults.update(overrides)
    return Task.objects.create(**defaults)


class QMonitorDegradesGracefullyTest(TestCase):
    """No cluster running / no relevant rows must not raise."""

    def test_cluster_stats_returns_list_without_active_cluster(self):
        # No qcluster heartbeat in the cache during tests — must degrade to [].
        self.assertEqual(qmonitor.cluster_stats(), [])

    def test_queue_depth_zero_when_orm_queue_empty(self):
        self.assertEqual(qmonitor.queue_depth(), 0)

    def test_upcoming_schedules_does_not_raise(self):
        # Some recurring Schedule rows (data-dump regen, weekly backfills, …) are
        # created automatically via post_migrate signals, so this isn't
        # necessarily empty — just must return a list without error.
        self.assertIsInstance(qmonitor.upcoming_schedules(), list)

    def test_upcoming_schedules_includes_a_freshly_created_schedule(self):
        Schedule.objects.create(func="works.tasks.some_task", name="qmonitor-test-schedule")
        labels = [s["label"] for s in qmonitor.upcoming_schedules(limit=100)]
        self.assertIn("qmonitor-test-schedule", labels)

    def test_upcoming_schedules_label_falls_back_to_func_when_name_blank(self):
        # Regression: schedules created via django_q.tasks.schedule() (the weekly
        # sweeps, data-dump regen, …) are never given an explicit name, which
        # rendered as a literal "None" in the dashboard's "Upcoming schedules" list.
        Schedule.objects.create(func="works.tasks.unnamed_schedule_test", name="")
        matching = [
            s for s in qmonitor.upcoming_schedules(limit=100) if s["func"] == "works.tasks.unnamed_schedule_test"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["label"], "works.tasks.unnamed_schedule_test")

    def test_recent_task_failures_empty_when_no_tasks(self):
        self.assertEqual(qmonitor.recent_task_failures(), [])

    def test_task_queryset_empty_when_no_tasks(self):
        self.assertEqual(list(qmonitor.task_queryset()), [])


class QMonitorTaskQuerysetTest(TestCase):
    def setUp(self):
        _make_task(id="a" * 32, name="task-a", func="works.tasks.harvest_oai_endpoint", success=True)
        _make_task(id="b" * 32, name="task-b", func="works.tasks.regenerate_all_data_dumps", success=False)

    def test_filter_by_status_success(self):
        results = list(qmonitor.task_queryset(status="success"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "task-a")

    def test_filter_by_status_failed(self):
        results = list(qmonitor.task_queryset(status="failed"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "task-b")

    def test_filter_by_func_substring(self):
        results = list(qmonitor.task_queryset(func="data_dumps"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "task-b")

    def test_recent_task_failures_lists_failure(self):
        failures = qmonitor.recent_task_failures()
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["name"], "task-b")


class QMonitorQueuedTasksTest(TestCase):
    """queued_tasks() — regression coverage for a task being genuinely
    enqueued (in OrmQ) but invisible via task_queryset()/Task history, since
    no worker has picked it up yet (all configured workers are busy with
    something else).
    """

    def test_empty_when_nothing_queued(self):
        self.assertEqual(qmonitor.queued_tasks(), [])

    def test_lists_a_genuinely_enqueued_task(self):
        from django_q.tasks import async_task

        async_task("works.tasks.backfill_work_countries", trigger_source="manual")

        queued = qmonitor.queued_tasks()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["func"], "works.tasks.backfill_work_countries")
        # Freshly enqueued (not yet claimed by a worker) — OrmQ.enqueue() stamps
        # `lock=now()`, so it's immediately "ready", not in the future.
        self.assertFalse(queued[0]["picked_up"])

    def test_task_claimed_by_a_worker_shows_as_picked_up(self):
        from django_q.models import OrmQ
        from django_q.tasks import async_task

        async_task("works.tasks.backfill_work_countries", trigger_source="manual")
        # Simulate a worker claiming the row: OrmQ's dequeue() pushes `lock`
        # into the future (now() + Q_CLUSTER["retry"]) as a processing lease.
        OrmQ.objects.update(lock=timezone.now() + timezone.timedelta(minutes=5))

        queued = qmonitor.queued_tasks()
        self.assertEqual(len(queued), 1)
        self.assertTrue(queued[0]["picked_up"])
        self.assertIsNotNone(queued[0]["locked_until"])

    def test_does_not_appear_in_task_history_queryset(self):
        # The bug this whole feature guards against: a queued-but-not-yet-run
        # task must not be confused with task_queryset()'s Task history — the
        # two are different tables, and only queued_tasks() sees this one.
        from django_q.tasks import async_task

        async_task("works.tasks.backfill_work_countries", trigger_source="manual")

        self.assertEqual(qmonitor.queued_tasks()[0]["func"], "works.tasks.backfill_work_countries")
        self.assertEqual(list(qmonitor.task_queryset()), [])

    def test_limit_is_respected(self):
        from django_q.tasks import async_task

        for _ in range(3):
            async_task("works.tasks.backfill_work_countries", trigger_source="manual")

        self.assertEqual(len(qmonitor.queued_tasks(limit=2)), 2)
        self.assertEqual(len(qmonitor.queued_tasks()), 3)

    def test_linkable_true_for_a_real_task_id(self):
        from django_q.tasks import async_task

        async_task("works.tasks.backfill_work_countries", trigger_source="manual")

        row = qmonitor.queued_tasks()[0]
        self.assertTrue(row["linkable"])
        self.assertTrue(row["task_id"])

    def test_get_queued_task_finds_a_waiting_task(self):
        from django_q.tasks import async_task

        async_task("works.tasks.backfill_work_countries", trigger_source="manual")
        task_id = qmonitor.queued_tasks()[0]["task_id"]

        found = qmonitor.get_queued_task(task_id)
        self.assertIsNotNone(found)
        self.assertEqual(found["func"], "works.tasks.backfill_work_countries")
        self.assertFalse(found["picked_up"])

    def test_get_queued_task_returns_none_for_unknown_id(self):
        self.assertIsNone(qmonitor.get_queued_task("does-not-exist"))


class ManageTasksViewsTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="tasks-staff@example.com", email="tasks-staff@example.com", password="p", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.staff)
        self.task = _make_task(
            id="c" * 32,
            name="task-c",
            func="works.tasks.some_task",
            success=False,
            result="Traceback: boom",
        )

    def test_tasks_list_renders(self):
        response = self.client.get(reverse("optimap:manage-tasks"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "works.tasks.some_task")

    def test_tasks_list_shows_queued_task(self):
        from django_q.tasks import async_task

        async_task("works.tasks.backfill_work_countries", trigger_source="manual")

        response = self.client.get(reverse("optimap:manage-tasks"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "works.tasks.backfill_work_countries")
        self.assertContains(response, "Waiting")

    def test_tasks_list_shows_no_queued_tasks_message(self):
        response = self.client.get(reverse("optimap:manage-tasks"))
        self.assertContains(response, "Nothing queued.")

    def test_tasks_list_header_says_queue_not_queued(self):
        # Regression: the section can contain a "Running" item, so a header
        # reading "Queued (N)" was misleading — it now reads "Queue (N)".
        response = self.client.get(reverse("optimap:manage-tasks"))
        self.assertContains(response, "Queue (0)")

    def test_tasks_list_has_history_subheading(self):
        response = self.client.get(reverse("optimap:manage-tasks"))
        self.assertContains(response, "Task history")

    def test_tasks_list_links_queued_task_to_its_detail_page(self):
        from django_q.tasks import async_task

        async_task("works.tasks.backfill_work_countries", trigger_source="manual")
        task_id = qmonitor.queued_tasks()[0]["task_id"]

        response = self.client.get(reverse("optimap:manage-tasks"))
        self.assertContains(response, reverse("optimap:manage-task-detail", args=[task_id]))

    def test_task_detail_renders(self):
        response = self.client.get(reverse("optimap:manage-task-detail", args=[self.task.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Traceback: boom")

    def test_task_detail_404_for_unknown_id(self):
        response = self.client.get(reverse("optimap:manage-task-detail", args=["z" * 32]))
        self.assertEqual(response.status_code, 404)

    def test_task_detail_falls_back_to_queued_entry_when_no_task_row_yet(self):
        from django_q.tasks import async_task

        async_task("works.tasks.backfill_work_countries", trigger_source="manual")
        task_id = qmonitor.queued_tasks()[0]["task_id"]

        response = self.client.get(reverse("optimap:manage-task-detail", args=[task_id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "works.tasks.backfill_work_countries")
        self.assertContains(response, "Waiting")
        self.assertContains(response, "no result yet")

    def test_anonymous_is_gated(self):
        self.client.logout()
        response = self.client.get(reverse("optimap:manage-tasks"))
        self.assertEqual(response.status_code, 302)
