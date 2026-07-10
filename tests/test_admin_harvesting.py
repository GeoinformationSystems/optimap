# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import os
from unittest.mock import patch

import django
import responses
from django.contrib import admin as admin_site
from django.test import RequestFactory, TestCase
from django.urls import reverse

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

from django.contrib.auth import get_user_model

from works.admin import (
    HarvestingEventAdmin,
    SourceAdmin,
    retry_event,
    schedule_harvesting,
    trigger_harvesting_for_specific,
)
from works.models import HarvestingEvent, Source
from works.tasks import harvest_oai_endpoint

User = get_user_model()


def _make_source(**overrides):
    defaults = dict(
        name="Test Source",
        url_field="http://example.org/oai",
        harvest_interval_minutes=60,
    )
    defaults.update(overrides)
    return Source.objects.create(**defaults)


class HarvestingEventLogPersistenceTest(TestCase):
    """The harvest_oai_endpoint task must persist log_text + counts/error on the event."""

    @responses.activate
    def test_failure_persists_error_and_log(self):
        source = _make_source(url_field="http://does-not-exist.invalid/oai")
        responses.add(
            responses.GET,
            "http://does-not-exist.invalid/oai",
            status=503,
            body="upstream down",
        )

        harvest_oai_endpoint(source.id, user=None)

        event = HarvestingEvent.objects.filter(source=source).latest("started_at")
        self.assertEqual(event.status, "failed")
        self.assertNotEqual(event.error_message, "")
        self.assertIn("503", event.error_message)
        self.assertNotEqual(event.log_text, "")
        self.assertIsNone(event.records_added)

    @responses.activate
    def test_failure_with_user_id_int_does_not_crash(self):
        # Regression: admin actions enqueue with user.id (int), and harvest_oai_endpoint
        # used to assume `user` was a User instance — `user.email` blew up with
        # AttributeError on the failure-notification branch. The task must accept an int.
        admin = User.objects.create_superuser(username="opsadmin", email="ops@example.org", password="x")
        source = _make_source(url_field="http://does-not-exist.invalid/oai")
        responses.add(
            responses.GET,
            "http://does-not-exist.invalid/oai",
            status=503,
            body="upstream down",
        )

        harvest_oai_endpoint(source.id, user=admin.id)

        event = HarvestingEvent.objects.filter(source=source).latest("started_at")
        self.assertEqual(event.status, "failed")

    @responses.activate
    def test_success_persists_counts_and_log(self):
        source = _make_source(url_field="http://example.org/oai")
        # Minimal valid OAI-PMH ListRecords response with zero records — exercises the
        # success branch without needing real publication parsing.
        empty_oai_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-04-30T00:00:00Z</responseDate>
  <ListRecords/>
</OAI-PMH>"""
        responses.add(
            responses.GET,
            "http://example.org/oai",
            body=empty_oai_xml,
            content_type="text/xml",
        )

        harvest_oai_endpoint(source.id, user=None)

        event = HarvestingEvent.objects.filter(source=source).latest("started_at")
        self.assertEqual(event.status, "completed")
        self.assertEqual(event.records_added, 0)
        self.assertEqual(event.records_with_spatial, 0)
        self.assertEqual(event.records_with_temporal, 0)
        # log_text always populated by HarvestWarningCollector.get_summary()
        self.assertNotEqual(event.log_text, "")
        # Regression: complete_harvest() must APPEND the final summary after
        # any incremental append_log() lines written during the run (the
        # per-year-chunk progress lines), not overwrite them — otherwise the
        # /manage/harvests/<id>/ live-log trail vanishes the moment the
        # harvest finishes.
        self.assertIn("Starting OAI-PMH harvest", event.log_text)
        self.assertIn("Checking year", event.log_text)


class SourceAdminRegistrationTest(TestCase):
    def test_source_is_registered_with_source_admin(self):
        self.assertIn(Source, admin_site.site._registry)
        self.assertIsInstance(admin_site.site._registry[Source], SourceAdmin)

    def test_harvestingevent_is_registered_with_event_admin(self):
        self.assertIn(HarvestingEvent, admin_site.site._registry)
        self.assertIsInstance(admin_site.site._registry[HarvestingEvent], HarvestingEventAdmin)

    def test_harvestingevent_admin_disallows_add(self):
        ma = admin_site.site._registry[HarvestingEvent]
        rf = RequestFactory()
        request = rf.get("/admin/works/harvestingevent/")
        self.assertFalse(ma.has_add_permission(request))


class HarvestAdminActionsTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username="admin", email="admin@example.org", password="x")
        self.source = _make_source()
        self.rf = RequestFactory()

    def _request(self):
        request = self.rf.post("/admin/works/source/")
        request.user = self.user
        # The action calls modeladmin.message_user; that needs a messages framework.
        # We bypass the storage by attaching a no-op _messages.
        from django.contrib.messages.storage.fallback import FallbackStorage

        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))
        return request

    @patch("works.admin.async_task")
    def test_trigger_action_enqueues_via_async_task_with_works_dotted_path(self, mock_async):
        ma = admin_site.site._registry[Source]
        request = self._request()
        trigger_harvesting_for_specific(ma, request, Source.objects.filter(id=self.source.id))

        self.assertEqual(mock_async.call_count, 1)
        args, _ = mock_async.call_args
        self.assertEqual(args[0], "works.tasks.harvest_oai_endpoint")
        self.assertEqual(args[1], self.source.id)

    def test_schedule_action_uses_works_dotted_path(self):
        ma = admin_site.site._registry[Source]
        request = self._request()
        # Clear any auto-created Schedule rows from Source.save().
        from django_q.models import Schedule

        Schedule.objects.filter(name=f"Manual Harvest Source {self.source.id}").delete()
        schedule_harvesting(ma, request, Source.objects.filter(id=self.source.id))

        sched = Schedule.objects.get(name=f"Manual Harvest Source {self.source.id}")
        self.assertEqual(sched.func, "works.tasks.harvest_oai_endpoint")

    @patch("works.admin.async_task")
    def test_trigger_action_dispatches_by_source_type_for_mountain_wetlands(self, mock_async):
        mwr = _make_source(
            name="MWR",
            url_field="https://andes.example.org/api/v1/items/",
            source_type="mountain-wetlands",
        )
        ma = admin_site.site._registry[Source]
        request = self._request()
        trigger_harvesting_for_specific(ma, request, Source.objects.filter(id=mwr.id))

        self.assertEqual(mock_async.call_count, 1)
        args, _ = mock_async.call_args
        self.assertEqual(args[0], "works.tasks.harvest_mountain_wetlands")
        self.assertEqual(args[1], mwr.id)

    @patch("works.admin.async_task")
    def test_trigger_action_dispatches_by_source_type_for_rss(self, mock_async):
        rss = _make_source(
            name="RSS",
            url_field="https://example.org/feed.rss",
            source_type="rss",
        )
        ma = admin_site.site._registry[Source]
        request = self._request()
        trigger_harvesting_for_specific(ma, request, Source.objects.filter(id=rss.id))

        self.assertEqual(mock_async.call_count, 1)
        args, _ = mock_async.call_args
        self.assertEqual(args[0], "works.tasks.harvest_rss_endpoint")

    def test_schedule_action_picks_task_by_source_type(self):
        from django_q.models import Schedule

        mwr = _make_source(
            name="MWR-sch",
            url_field="https://andes.example.org/api/v1/items2/",
            source_type="mountain-wetlands",
        )
        ma = admin_site.site._registry[Source]
        request = self._request()
        Schedule.objects.filter(name=f"Manual Harvest Source {mwr.id}").delete()
        schedule_harvesting(ma, request, Source.objects.filter(id=mwr.id))

        sched = Schedule.objects.get(name=f"Manual Harvest Source {mwr.id}")
        self.assertEqual(sched.func, "works.tasks.harvest_mountain_wetlands")

    @patch("works.admin.async_task")
    def test_retry_event_dispatches_by_current_source_type(self, mock_async):
        mwr = _make_source(
            name="MWR-retry",
            url_field="https://andes.example.org/api/v1/items3/",
            source_type="mountain-wetlands",
        )
        event = HarvestingEvent.objects.create(source=mwr, status="failed")
        ma = admin_site.site._registry[HarvestingEvent]
        request = self.rf.post("/admin/works/harvestingevent/")
        request.user = self.user
        from django.contrib.messages.storage.fallback import FallbackStorage

        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))

        retry_event(ma, request, HarvestingEvent.objects.filter(id=event.id))

        self.assertEqual(mock_async.call_count, 1)
        args, _ = mock_async.call_args
        self.assertEqual(args[0], "works.tasks.harvest_mountain_wetlands")

    @patch("works.admin.async_task")
    def test_retry_event_action_enqueues_per_event(self, mock_async):
        ma = admin_site.site._registry[HarvestingEvent]
        event = HarvestingEvent.objects.create(source=self.source, status="failed")
        request = self.rf.post("/admin/works/harvestingevent/")
        request.user = self.user
        from django.contrib.messages.storage.fallback import FallbackStorage

        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))

        retry_event(ma, request, HarvestingEvent.objects.filter(id=event.id))

        self.assertEqual(mock_async.call_count, 1)
        args, _ = mock_async.call_args
        self.assertEqual(args[0], "works.tasks.harvest_oai_endpoint")
        self.assertEqual(args[1], self.source.id)


class HarvestingEventAdminChangeViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username="admin", email="admin@example.org", password="x")
        self.client.force_login(self.user)
        self.source = _make_source()
        self.event = HarvestingEvent.objects.create(
            source=self.source,
            status="failed",
            error_message="Boom: upstream returned 503",
            log_text="🔴 ERROR: something blew up\n🟡 WARNING: also this",
            records_added=None,
        )

    def test_change_view_renders_log_and_error(self):
        url = reverse("admin:works_harvestingevent_change", args=[self.event.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Boom: upstream returned 503")
        self.assertContains(response, "something blew up")

    def test_changelist_renders(self):
        url = reverse("admin:works_harvestingevent_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class SourceScheduleTest(TestCase):
    """Source.save() must not queue every source to fire immediately."""

    def test_first_run_is_deferred_by_one_interval(self):
        # Regression: Schedule.next_run defaults to timezone.now, so brand-new
        # sources (e.g. those bulk-created by `--insert-sources`) all fired on
        # the next cluster tick. Source.save() must set next_run = now + interval.
        from datetime import timedelta

        from django.utils import timezone
        from django_q.models import Schedule

        before = timezone.now()
        source = _make_source(name="Deferred", harvest_interval_minutes=120)
        sched = Schedule.objects.get(name=f"Harvest Source {source.id}")

        expected_floor = before + timedelta(minutes=120)
        self.assertGreaterEqual(
            sched.next_run,
            expected_floor,
            "next_run must be at least one interval in the future after Source.save()",
        )

    def test_unrelated_save_does_not_reset_next_run(self):
        # Regression: Source.save() previously did delete-and-recreate on every save,
        # which reset next_run to "now" on any unrelated edit (e.g. last_harvest).
        from django_q.models import Schedule

        source = _make_source(name="Stable", harvest_interval_minutes=120)
        sched = Schedule.objects.get(name=f"Harvest Source {source.id}")
        original_next_run = sched.next_run
        original_id = sched.id

        source.publisher_name = "Some Publisher"
        source.save()

        sched_after = Schedule.objects.get(name=f"Harvest Source {source.id}")
        self.assertEqual(sched_after.id, original_id, "schedule row must be preserved")
        self.assertEqual(sched_after.next_run, original_next_run)

    def test_interval_change_recreates_schedule(self):
        from django_q.models import Schedule

        source = _make_source(name="Reinterval", harvest_interval_minutes=120)
        original_id = Schedule.objects.get(name=f"Harvest Source {source.id}").id

        source.harvest_interval_minutes = 60
        source.save()

        sched = Schedule.objects.get(name=f"Harvest Source {source.id}")
        self.assertNotEqual(sched.id, original_id)
        self.assertEqual(sched.minutes, 60)


class SourceAdminChangelistTest(TestCase):
    """The Source changelist must render with sources that have / don't have events."""

    def setUp(self):
        self.user = User.objects.create_superuser(username="admin", email="admin@example.org", password="x")
        self.client.force_login(self.user)

    def test_changelist_renders_when_source_has_events(self):
        # Regression: latest_event_status used format_html with a strftime spec, which
        # ValueError'd because format_html stringifies args before applying the spec.
        source = _make_source(name="With Events")
        HarvestingEvent.objects.create(source=source, status="completed")

        url = reverse("admin:works_source_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "With Events")

    def test_changelist_renders_when_source_has_no_events(self):
        _make_source(name="No Events")
        url = reverse("admin:works_source_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Events")


class StaleUserForeignKeyResilienceTest(TestCase):
    """A HarvestingEvent whose `user` was deleted mid-run (e.g. concurrently by
    something else, or a long-running worker holding a stale in-memory copy)
    must not crash logging or the terminal-state write.

    Regression: Django's Model.save() refuses to write a row whose cached
    related-object (here `user`) was deleted ("save() prohibited to prevent
    data loss"), which used to propagate out of append_log()/complete_harvest()/
    fail_harvest() and crash an otherwise-successful harvest, leaving the event
    stuck without ever being marked completed or failed.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="stale-fk@example.com", email="stale-fk@example.com")
        self.source = _make_source(name="Stale FK Source", url_field="http://stale-fk.example.org/oai")
        # Passing the instance (not user_id=) caches it as the FK's related
        # object; deleting that same instance afterwards sets its .pk to None,
        # which is exactly what Django's save()-time guard checks for.
        self.event = HarvestingEvent.objects.create(source=self.source, user=self.user, status="in_progress")
        self.user.delete()

    def test_append_log_does_not_raise_after_user_deleted(self):
        # The line is still set in memory (harmless), but since the row can't
        # be saved with this stale FK, append_log() drops it rather than
        # crashing — it's only ever persisted by the next successful save
        # (e.g. complete_harvest()/fail_harvest(), which have their own
        # queryset-update() fallback and so still land in the DB).
        self.event.append_log("line written after user deletion")  # must not raise
        self.assertIn("line written after user deletion", self.event.log_text)

    def test_complete_harvest_persists_status_despite_stale_user(self):
        from works.harvesting.common import HarvestStats, HarvestWarningCollector, complete_harvest

        complete_harvest(self.event, HarvestStats(), HarvestWarningCollector())  # must not raise
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, "completed")
        self.assertIsNone(self.event.user_id)

    def test_fail_harvest_persists_status_despite_stale_user(self):
        from works.harvesting.common import HarvestWarningCollector, fail_harvest

        fail_harvest(self.event, RuntimeError("boom"), HarvestWarningCollector())  # must not raise
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, "failed")
        self.assertIn("boom", self.event.error_message)


class LogWorkBatchingTest(TestCase):
    """HarvestingEvent.log_work() batches its DB write every 5 calls rather
    than writing on every call — a full DB round-trip per record would
    multiply write volume by the record count on large harvests.
    """

    def setUp(self):
        self.source = _make_source(name="Log Work Source")
        self.event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

    def test_lines_are_buffered_in_memory_before_flush(self):
        for i in range(4):
            self.event.log_work(f"line {i}")
        # In-memory copy always has every line...
        for i in range(4):
            self.assertIn(f"line {i}", self.event.log_text)
        # ...but nothing has been flushed to the DB yet.
        stored = HarvestingEvent.objects.get(pk=self.event.pk)
        self.assertEqual(stored.log_text, "")

    def test_fifth_call_flushes_all_buffered_lines(self):
        for i in range(5):
            self.event.log_work(f"line {i}")
        stored = HarvestingEvent.objects.get(pk=self.event.pk)
        for i in range(5):
            self.assertIn(f"line {i}", stored.log_text)

    def test_buffered_lines_survive_to_complete_harvest(self):
        # Lines buffered-but-not-yet-flushed must still make it into the DB
        # once the run ends — complete_harvest()/fail_harvest() persist
        # whatever is in self.log_text regardless of the flush counter.
        from works.harvesting.common import HarvestStats, HarvestWarningCollector, complete_harvest

        self.event.log_work("only line")  # 1 of 5 — not flushed yet
        complete_harvest(self.event, HarvestStats(), HarvestWarningCollector())
        stored = HarvestingEvent.objects.get(pk=self.event.pk)
        self.assertIn("only line", stored.log_text)

    def test_log_text_is_capped_rather_than_growing_unboundedly(self):
        # A harvest logging one line per work (tens of thousands of records
        # for some sources) must not let log_text grow without bound — every
        # save() of the row would otherwise resend an ever-larger string.
        for i in range(2000):
            self.event.append_log(f"line number {i} with some padding text to bulk it out")
        self.assertLessEqual(len(self.event.log_text), HarvestingEvent._LOG_TEXT_MAX_CHARS + 200)
        # The most recent line must still be present (front gets trimmed, not the tail).
        self.assertIn("line number 1999", self.event.log_text)
        self.assertIn("truncated", self.event.log_text)


class LogWorkPerWorkHelpersTest(TestCase):
    """works.harvesting.common.log_work_retrieved()/log_work_enriched()/
    log_work_failed() — the per-harvester "Retrieved"/"Enriched"/"Failed to
    save" checkpoints, shared across all 8 harvesters.
    """

    def setUp(self):
        self.source = _make_source(name="Per-Work Helpers Source")
        self.event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

    def test_retrieved_line(self):
        from works.harvesting.common import log_work_retrieved

        log_work_retrieved(self.event, "A Retrieved Title")
        self.assertIn("Retrieved: A Retrieved Title", self.event.log_text)

    def test_retrieved_line_falls_back_when_title_missing(self):
        from works.harvesting.common import log_work_retrieved

        log_work_retrieved(self.event, None)
        self.assertIn("Retrieved: No title", self.event.log_text)

    def test_enriched_line_matched(self):
        from works.harvesting.common import log_work_enriched

        log_work_enriched(self.event, "A Title", {"openalex_id": "W123"})
        self.assertIn("Enriched: A Title — OpenAlex matched", self.event.log_text)

    def test_enriched_line_no_match(self):
        from works.harvesting.common import log_work_enriched

        log_work_enriched(self.event, "A Title", {"openalex_id": None})
        self.assertIn("Enriched: A Title — OpenAlex no match", self.event.log_text)

    def test_failed_line_includes_exception(self):
        from works.harvesting.common import log_work_failed

        log_work_failed(self.event, "A Title", RuntimeError("db exploded"))
        self.assertIn("Failed to save: A Title — db exploded", self.event.log_text)

    def test_all_three_are_no_ops_without_an_event(self):
        from works.harvesting.common import log_work_enriched, log_work_failed, log_work_retrieved

        log_work_retrieved(None, "x")  # must not raise
        log_work_enriched(None, "x", {})  # must not raise
        log_work_failed(None, "x", RuntimeError("boom"))  # must not raise


class LogWorkOutcomeTest(TestCase):
    """works.harvesting.common.log_work_outcome()/log_work_skip() — the shared
    per-work UI log lines emitted from _save_or_update_work() and each
    harvester's early-dedup check.
    """

    def setUp(self):
        self.source = _make_source(name="Outcome Source")
        self.event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

    def test_created_line_includes_work_id_and_no_enrichment_note_when_absent(self):
        from works.harvesting.common import log_work_outcome

        work = type("W", (), {"id": 42})()
        log_work_outcome(self.event, {"title": "A Sample Title"}, work, "created")
        self.assertIn("Saved id=42: A Sample Title", self.event.log_text)
        self.assertNotIn("OpenAlex", self.event.log_text)

    def test_enrichment_note_reflects_openalex_match(self):
        from works.harvesting.common import log_work_outcome

        work = type("W", (), {"id": 7})()
        log_work_outcome(self.event, {"title": "Matched Work", "openalex_id": "W123"}, work, "created")
        self.assertIn("Saved id=7: Matched Work (OpenAlex: matched)", self.event.log_text)

    def test_enrichment_note_reflects_no_openalex_match(self):
        from works.harvesting.common import log_work_outcome

        work = type("W", (), {"id": 8})()
        log_work_outcome(self.event, {"title": "Unmatched Work", "openalex_id": None}, work, "updated")
        self.assertIn("Updated id=8: Unmatched Work (OpenAlex: no match)", self.event.log_text)

    def test_skipped_same_source_line(self):
        from works.harvesting.common import log_work_outcome

        work = type("W", (), {"id": 9})()
        log_work_outcome(self.event, {"title": "Dup Work"}, work, "skipped_same_source")
        self.assertIn("Skipped (duplicate) id=9: Dup Work", self.event.log_text)

    def test_skipped_cross_source_line(self):
        from works.harvesting.common import log_work_outcome

        work = type("W", (), {"id": 10})()
        log_work_outcome(self.event, {"title": "Cross Source Work"}, work, "skipped_cross_source")
        self.assertIn("Skipped (already under another source) id=10: Cross Source Work", self.event.log_text)

    def test_no_event_is_a_no_op(self):
        from works.harvesting.common import log_work_outcome

        log_work_outcome(None, {"title": "x"}, None, "created")  # must not raise

    def test_log_work_skip_uses_matching_labels(self):
        from works.harvesting.common import log_work_skip

        log_work_skip(self.event, "Early Duplicate", cross_source=False)
        self.assertIn("Skipped (duplicate): Early Duplicate", self.event.log_text)
        log_work_skip(self.event, "Early Cross-Source Duplicate", cross_source=True)
        self.assertIn("Skipped (already under another source): Early Cross-Source Duplicate", self.event.log_text)


class DuplicateRetryOnTerminalEventTest(TestCase):
    """harvest_oai_endpoint (representative of all 9 harvest_* entry points)
    must abort cleanly, doing no work, when its event_id already points to a
    terminal HarvestingEvent — the signature of Django-Q re-executing an
    already-concluded task (its own retry-on-timeout/failure mechanism).

    Regression: this used to silently create a *second* HarvestingEvent
    mislabeled trigger_source="schedule" (the model default, since a retry
    never passes trigger_source) and re-run the whole harvest — turning one
    stuck task into an unbounded stream of phantom "scheduled" harvests.
    """

    def test_harvest_oai_endpoint_does_nothing_when_event_already_completed(self):
        source = _make_source(name="Already Done Source", url_field="http://example.org/already-done")
        done = HarvestingEvent.objects.create(source=source, status="completed", trigger_source="management-command")
        count_before = HarvestingEvent.objects.count()

        harvest_oai_endpoint(source.id, event_id=done.id)  # must not raise, must not fetch anything

        self.assertEqual(HarvestingEvent.objects.count(), count_before)
        done.refresh_from_db()
        self.assertEqual(done.status, "completed")  # untouched


class HarvestEventUrlTest(TestCase):
    """render_harvest_email(event=...) links a "Harvesting Completed" email
    back to that run's /manage/harvests/<id>/ page."""

    def test_harvest_event_url_builds_absolute_manage_link(self):
        from django.conf import settings

        from works.harvesting.common import harvest_event_url

        source = _make_source()
        event = HarvestingEvent.objects.create(source=source, status="completed")

        url = harvest_event_url(event)

        self.assertEqual(url, f"{settings.BASE_URL}{reverse('optimap:manage-harvest-detail', args=[event.id])}")

    def test_render_harvest_email_injects_detail_url_from_event(self):
        from works.harvesting.common import harvest_event_url, render_harvest_email

        source = _make_source()
        event = HarvestingEvent.objects.create(source=source, status="completed")

        _subject, body = render_harvest_email(
            "email/harvest_success.en.txt",
            {
                "subject_prefix": "",
                "source_label": source.name,
                "detail_header": "Test harvest details:",
                "records_added_label": "New works",
                "records_added": 0,
                "records_updated_label": "Updated works",
                "records_updated": 0,
                "spatial_label": "With spatial extent",
                "spatial_count": 0,
                "temporal_label": "With temporal extent",
                "temporal_count": 0,
                "event_started": "2026-01-01 00:00:00",
                "event_completed": "2026-01-01 00:01:00",
            },
            event=event,
        )

        self.assertIn(harvest_event_url(event), body)

    def test_render_harvest_email_does_not_override_an_explicit_detail_url(self):
        from works.harvesting.common import render_harvest_email

        source = _make_source()
        event = HarvestingEvent.objects.create(source=source, status="completed")

        _subject, body = render_harvest_email(
            "email/harvest_success.en.txt",
            {
                "subject_prefix": "",
                "source_label": source.name,
                "detail_header": "Test harvest details:",
                "records_added_label": "New works",
                "records_added": 0,
                "records_updated_label": "Updated works",
                "records_updated": 0,
                "spatial_label": "With spatial extent",
                "spatial_count": 0,
                "temporal_label": "With temporal extent",
                "temporal_count": 0,
                "event_started": "2026-01-01 00:00:00",
                "event_completed": "2026-01-01 00:01:00",
                "detail_url": "https://explicit.example/override",
            },
            event=event,
        )

        self.assertIn("https://explicit.example/override", body)
