# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for /manage/harvests/ — triggering, filtering, live-log polling JSON,
and the HarvestRecord created/updated lists (issues #133, #44)."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import GeometryCollection, Point
from django.test import Client, TestCase
from django.urls import reverse

from works.harvesting.common import HarvestStats, _save_or_update_work
from works.models import HarvestingEvent, HarvestRecord, Source, Work

User = get_user_model()


def _make_source(**overrides):
    defaults = dict(name="Test Source", url_field="http://example.org/oai", source_type="oai-pmh")
    defaults.update(overrides)
    return Source.objects.create(**defaults)


class ManageHarvestsListTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff@example.com", email="staff@example.com", password="p", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.staff)
        self.source = _make_source()

    def test_list_renders_events(self):
        HarvestingEvent.objects.create(source=self.source, status="completed", trigger_source="management-command")
        response = self.client.get(reverse("optimap:manage-harvests"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.source.name)

    def test_non_numeric_source_filter_does_not_crash(self):
        # Regression: ?source=<non-numeric> used to raise an uncaught ValueError
        # when passed straight into .filter(source_id=...).
        response = self.client.get(reverse("optimap:manage-harvests"), {"source": "not-a-number"})
        self.assertEqual(response.status_code, 200)

    def test_filter_by_status_and_trigger_source(self):
        HarvestingEvent.objects.create(source=self.source, status="completed", trigger_source="ui")
        HarvestingEvent.objects.create(source=self.source, status="failed", trigger_source="schedule")
        response = self.client.get(reverse("optimap:manage-harvests"), {"status": "failed"})
        # The status <select> always lists every choice (including "Completed"),
        # so assert on the event row's badge class rather than the bare word.
        self.assertContains(response, "badge-danger")
        self.assertNotContains(response, "badge-success")


class ManageTriggerHarvestTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="trigger-staff@example.com", email="trigger-staff@example.com", password="p", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.staff)
        self.source = _make_source()

    @patch("works.views_manage.async_task")
    def test_trigger_creates_pending_event_and_enqueues_task(self, mock_async):
        mock_async.return_value = "fake-task-id"
        response = self.client.post(
            reverse("optimap:manage-harvests"), {"source": self.source.id, "max_records": "10"}
        )
        self.assertEqual(response.status_code, 302)

        event = HarvestingEvent.objects.filter(source=self.source).latest("started_at")
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.trigger_source, "ui")
        self.assertEqual(event.user, self.staff)
        self.assertIn(reverse("optimap:manage-harvest-detail", args=[event.id]), response.headers["Location"])

        mock_async.assert_called_once()
        args, kwargs = mock_async.call_args
        self.assertEqual(args[0], "works.tasks.harvest_oai_endpoint")
        self.assertEqual(args[1], self.source.id)
        # No trigger_source kwarg on the task call itself — the pre-created
        # event (asserted above) already carries it, and start_harvesting_event
        # reuses that row via event_id without reading a trigger_source kwarg.
        self.assertNotIn("trigger_source", kwargs)
        self.assertEqual(kwargs["max_records"], 10)
        self.assertEqual(kwargs["event_id"], event.id)

    @patch("works.views_manage.async_task")
    def test_trigger_without_registered_harvester_shows_error(self, mock_async):
        no_task_source = _make_source(name="No Task", source_type="does-not-exist")
        response = self.client.post(reverse("optimap:manage-harvests"), {"source": no_task_source.id})
        self.assertEqual(response.status_code, 302)
        mock_async.assert_not_called()

    @patch("works.views_manage.async_task")
    def test_non_numeric_source_does_not_crash(self, mock_async):
        # Regression: get_object_or_404(Source, pk=<non-numeric>) raises an
        # uncaught ValueError (only DoesNotExist is caught), not a clean 404.
        response = self.client.post(reverse("optimap:manage-harvests"), {"source": "not-a-number"})
        self.assertEqual(response.status_code, 302)
        mock_async.assert_not_called()


class ManageHarvestDetailJsonPollTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="poll-staff@example.com", email="poll-staff@example.com", password="p", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.staff)
        self.source = _make_source()
        self.event = HarvestingEvent.objects.create(source=self.source, status="in_progress", trigger_source="ui")

    def test_detail_page_renders(self):
        response = self.client.get(reverse("optimap:manage-harvest-detail", args=[self.event.id]))
        self.assertEqual(response.status_code, 200)

    def test_json_poll_returns_new_log_tail_since_offset(self):
        self.event.append_log("first line")
        self.event.append_log("second line")
        self.event.refresh_from_db()
        first_len = len(self.event.log_text.split("second line")[0])

        response = self.client.get(
            reverse("optimap:manage-harvest-detail", args=[self.event.id]),
            {"format": "json", "since": first_len},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("second line", data["log_tail"])
        self.assertNotIn("first line", data["log_tail"])
        self.assertEqual(data["status"], "in_progress")

    def test_json_poll_reports_terminal_status(self):
        from works.harvesting.common import HarvestWarningCollector, complete_harvest

        stats = HarvestStats()
        complete_harvest(self.event, stats, HarvestWarningCollector())
        self.event.refresh_from_db()

        response = self.client.get(
            reverse("optimap:manage-harvest-detail", args=[self.event.id]), {"format": "json", "since": 0}
        )
        data = response.json()
        self.assertEqual(data["status"], "completed")
        self.assertIsNotNone(data["completed_at"])

    def test_json_poll_reports_live_counts_while_running(self):
        # Regression: HarvestingEvent.records_added/etc. are only written once,
        # at the very end (complete_harvest()) — while still in_progress they
        # must be derived live from HarvestRecord, not read as "—"/None, even
        # though the newly-harvested list (same underlying data) already shows
        # the works.
        _save_or_update_work(
            {"title": "Live Count Work", "doi": "10.1234/live-count-1", "source": self.source, "job": self.event},
            self.source,
            self.event,
        )

        response = self.client.get(
            reverse("optimap:manage-harvest-detail", args=[self.event.id]), {"format": "json", "since": 0}
        )
        data = response.json()
        self.assertEqual(data["records_added"], 1)
        self.assertEqual(data["records_updated"], 0)

    def test_json_poll_does_not_count_no_temporal_data_as_temporal(self):
        # Regression: extract_timeperiod_from_html() returns [None]/[None] both
        # for a genuine open-ended interval (one side real) and for "no
        # temporal metadata found on the page at all" — a work with the latter
        # must not be counted as "with temporal extent" just because the
        # arrays are non-empty.
        Work.objects.create(
            title="No real temporal data",
            doi="10.1234/no-temporal-1",
            source=self.source,
            job=self.event,
            timeperiod_startdate=[None],
            timeperiod_enddate=[None],
        )
        Work.objects.create(
            title="Genuine open-ended interval",
            doi="10.1234/open-interval-1",
            source=self.source,
            job=self.event,
            timeperiod_startdate=[None],
            timeperiod_enddate=["2024-12-31"],
        )

        response = self.client.get(
            reverse("optimap:manage-harvest-detail", args=[self.event.id]), {"format": "json", "since": 0}
        )
        data = response.json()
        self.assertEqual(data["records_with_temporal"], 1)

    def test_json_poll_does_not_count_empty_geometry_as_spatial(self):
        # Regression: a saved-but-empty GeometryCollection() (the harvesters'
        # placeholder when no geometry was extracted) is non-null, so a bare
        # `exclude(geometry__isnull=True)` miscounted it as "with spatial
        # extent" — inflating the stat far past what the corresponding
        # collection page (which correctly checks `.empty`) shows.
        Work.objects.create(
            title="No real geometry",
            doi="10.1234/empty-geom-1",
            source=self.source,
            job=self.event,
            geometry=GeometryCollection(),
        )
        Work.objects.create(
            title="Has real geometry",
            doi="10.1234/real-geom-1",
            source=self.source,
            job=self.event,
            geometry=GeometryCollection(Point(1.0, 2.0)),
        )

        response = self.client.get(
            reverse("optimap:manage-harvest-detail", args=[self.event.id]), {"format": "json", "since": 0}
        )
        data = response.json()
        self.assertEqual(data["records_with_spatial"], 1)

    def test_json_poll_includes_created_records_while_running(self):
        _save_or_update_work(
            {"title": "Poll List Work", "doi": "10.1234/poll-list-1", "source": self.source, "job": self.event},
            self.source,
            self.event,
        )

        response = self.client.get(
            reverse("optimap:manage-harvest-detail", args=[self.event.id]), {"format": "json", "since": 0}
        )
        data = response.json()
        self.assertEqual(len(data["created_records"]), 1)
        self.assertEqual(data["created_records"][0]["title"], "Poll List Work")
        self.assertIn("/", data["created_records"][0]["url"])
        self.assertEqual(data["updated_records"], [])

    def test_json_poll_omits_record_lists_once_terminal(self):
        from works.harvesting.common import HarvestWarningCollector, complete_harvest

        complete_harvest(self.event, HarvestStats(), HarvestWarningCollector())
        self.event.refresh_from_db()

        response = self.client.get(
            reverse("optimap:manage-harvest-detail", args=[self.event.id]), {"format": "json", "since": 0}
        )
        data = response.json()
        self.assertNotIn("created_records", data)
        self.assertNotIn("updated_records", data)


class HarvestRecordCreationTest(TestCase):
    """_save_or_update_work must write a HarvestRecord row for created/updated works."""

    def setUp(self):
        self.source = _make_source()
        self.event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

    def test_created_work_gets_harvest_record(self):
        work, action = _save_or_update_work(
            {"title": "New Work", "doi": "10.1234/hr-created", "source": self.source, "job": self.event},
            self.source,
            self.event,
        )
        self.assertEqual(action, "created")
        record = HarvestRecord.objects.get(event=self.event, work=work)
        self.assertEqual(record.action, "created")

    def test_updated_work_gets_harvest_record(self):
        existing = Work.objects.create(title="Existing", doi="10.1234/hr-updated", source=self.source)
        work, action = _save_or_update_work(
            {"title": "Existing Updated", "doi": "10.1234/hr-updated", "source": self.source, "job": self.event},
            self.source,
            self.event,
            update_existing=True,
        )
        self.assertEqual(action, "updated")
        self.assertEqual(work.id, existing.id)
        record = HarvestRecord.objects.get(event=self.event, work=work)
        self.assertEqual(record.action, "updated")

    def test_skipped_duplicate_gets_no_harvest_record(self):
        Work.objects.create(title="Existing", doi="10.1234/hr-skip", source=self.source)
        _, action = _save_or_update_work(
            {"title": "Existing", "doi": "10.1234/hr-skip", "source": self.source, "job": self.event},
            self.source,
            self.event,
            update_existing=False,
        )
        self.assertEqual(action, "skipped_same_source")
        self.assertFalse(HarvestRecord.objects.filter(event=self.event).exists())


class ManageCancelQueuedHarvestTest(TestCase):
    """POST /manage/harvests/<id>/ cancels a still-pending (queued) harvest."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username="cancel-staff@example.com", email="cancel-staff@example.com", password="p", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.staff)
        self.source = _make_source()

    def test_cancel_pending_event_removes_queued_task(self):
        from django_q.models import OrmQ
        from django_q.tasks import async_task

        event = HarvestingEvent.objects.create(source=self.source, status="pending", trigger_source="ui")
        async_task("works.tasks.harvest_oai_endpoint", self.source.id, event_id=event.id)
        self.assertTrue(OrmQ.objects.filter(payload__isnull=False).exists())

        response = self.client.post(reverse("optimap:manage-harvest-detail", args=[event.id]))
        self.assertEqual(response.status_code, 302)

        event.refresh_from_db()
        self.assertEqual(event.status, "cancelled")
        self.assertIn("Cancelled by staff", event.error_message)
        self.assertIsNotNone(event.completed_at)
        # The matching queued task must be gone so the worker never picks it up.
        from works.qmonitor import find_queued_task_for_event

        self.assertIsNone(find_queued_task_for_event(event.id))

    def test_cancel_refuses_non_pending_event(self):
        event = HarvestingEvent.objects.create(source=self.source, status="in_progress", trigger_source="ui")
        response = self.client.post(reverse("optimap:manage-harvest-detail", args=[event.id]))
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.status, "in_progress")

    def test_cancel_pending_event_with_no_queued_task_still_marks_cancelled(self):
        # e.g. the task was already picked up a moment ago — no OrmQ row left.
        event = HarvestingEvent.objects.create(source=self.source, status="pending", trigger_source="ui")
        response = self.client.post(reverse("optimap:manage-harvest-detail", args=[event.id]))
        self.assertEqual(response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.status, "cancelled")

    def test_cancel_button_shown_only_for_pending(self):
        pending = HarvestingEvent.objects.create(source=self.source, status="pending", trigger_source="ui")
        running = HarvestingEvent.objects.create(source=self.source, status="in_progress", trigger_source="ui")
        r1 = self.client.get(reverse("optimap:manage-harvest-detail", args=[pending.id]))
        r2 = self.client.get(reverse("optimap:manage-harvest-detail", args=[running.id]))
        self.assertContains(r1, "Cancel this queued harvest")
        self.assertNotContains(r2, "Cancel this queued harvest")


class HarvestingEventStalenessTest(TestCase):
    """HarvestingEvent.is_stale — an in_progress event older than Django-Q's
    own task timeout (Q_CLUSTER["timeout"]), or a pending event older than the
    much shorter HARVEST_PENDING_STALE_MINUTES, is reliably orphaned, not just
    "running/queued a while" (issue #130 follow-up)."""

    def setUp(self):
        self.source = _make_source()

    def _make_event(self, status, age_seconds):
        from datetime import timedelta

        from django.utils import timezone

        event = HarvestingEvent.objects.create(source=self.source, status=status, trigger_source="ui")
        HarvestingEvent.objects.filter(pk=event.pk).update(started_at=timezone.now() - timedelta(seconds=age_seconds))
        event.refresh_from_db()
        return event

    def test_recent_in_progress_event_is_not_stale(self):
        event = self._make_event("in_progress", age_seconds=30)
        self.assertFalse(event.is_stale)
        self.assertEqual(event.staleness_message, "")

    def test_old_in_progress_event_is_stale(self):
        from django.conf import settings

        event = self._make_event("in_progress", age_seconds=settings.Q_CLUSTER["timeout"] + 60)
        self.assertTrue(event.is_stale)
        self.assertIn("timeout", event.staleness_message)

    def test_recent_pending_event_is_not_stale(self):
        event = self._make_event("pending", age_seconds=30)
        self.assertFalse(event.is_stale)
        self.assertEqual(event.staleness_message, "")

    def test_old_pending_event_is_stale_with_queue_message(self):
        from django.conf import settings

        event = self._make_event("pending", age_seconds=(settings.HARVEST_PENDING_STALE_MINUTES * 60) + 60)
        self.assertTrue(event.is_stale)
        self.assertIn("queued", event.staleness_message)

    def test_old_completed_event_is_never_stale(self):
        from django.conf import settings

        event = self._make_event("completed", age_seconds=settings.Q_CLUSTER["timeout"] + 60)
        self.assertFalse(event.is_stale)


class ManageStalenessDisplayTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="stale-staff@example.com", email="stale-staff@example.com", password="p", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.staff)
        self.source = _make_source()

    def _old_in_progress_event(self):
        from datetime import timedelta

        from django.conf import settings
        from django.utils import timezone

        event = HarvestingEvent.objects.create(source=self.source, status="in_progress", trigger_source="ui")
        HarvestingEvent.objects.filter(pk=event.pk).update(
            started_at=timezone.now() - timedelta(seconds=settings.Q_CLUSTER["timeout"] + 60)
        )
        return event

    def test_harvest_detail_shows_staleness_warning(self):
        event = self._old_in_progress_event()
        response = self.client.get(reverse("optimap:manage-harvest-detail", args=[event.id]))
        self.assertContains(response, "staleness-warning")

    def test_harvests_list_shows_staleness_icon(self):
        self._old_in_progress_event()
        response = self.client.get(reverse("optimap:manage-harvests"))
        self.assertContains(response, "exclamation-triangle")

    def test_dashboard_json_reports_staleness(self):
        event = self._old_in_progress_event()
        response = self.client.get(reverse("optimap:manage-dashboard"), {"format": "json"})
        data = response.json()
        matching = [e for e in data["running_harvests"] if e["id"] == event.id]
        self.assertEqual(len(matching), 1)
        self.assertTrue(matching[0]["is_stale"])
        self.assertIn("timeout", matching[0]["staleness_message"])

    def test_harvest_detail_json_reports_staleness(self):
        event = self._old_in_progress_event()
        response = self.client.get(
            reverse("optimap:manage-harvest-detail", args=[event.id]), {"format": "json", "since": 0}
        )
        data = response.json()
        self.assertTrue(data["is_stale"])
