# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for OpenAIRE enrichment (a second enrichment source besides OpenAlex)."""

import os
from io import StringIO
from unittest.mock import MagicMock, patch

import django
import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings, tag

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

from django.core.management import call_command

from works.harvesting.common import (
    HarvestStats,
    HarvestWarningCollector,
    _carefully_update_work,
    complete_harvest,
)
from works.harvesting.enrichment import apply_enrichment
from works.harvesting.openaire import (
    OpenAireUnavailable,
    build_openaire_fields,
    enrich_event_from_openaire,
    enrich_work_from_openaire,
    fetch_openaire_record,
)
from works.models import HarvestingEvent, Source, Work

# A representative OpenAIRE Graph API record (shape verified live for
# 10.1007/978-3-540-78946-8_4). Two descriptions so the "longest wins" rule is
# exercised; subjects mix free-text keywords with automated SDG/FOS schemes.
FAKE_RECORD = {
    "id": "doi_dedup___::abc123",
    "mainTitle": "Evaluation of the Geometric Accuracy of 3D City Models",
    "descriptions": [
        "Short note.",
        "A considerably longer abstract that describes the methods and results in full and should be the one selected.",
    ],
    "authors": [
        {"fullName": "Gerald Gruber", "rank": 1},
        {"fullName": "Christian Menard", "rank": 2},
        {"fullName": "", "rank": 3},
    ],
    "subjects": [
        {"subject": {"scheme": "SDG", "value": "11. Sustainability"}},
        {"subject": {"scheme": "keyword", "value": "city models"}},
        {"subject": {"scheme": "", "value": "accuracy"}},
        {"subject": {"scheme": "FOS", "value": "Engineering"}},
    ],
    "container": {"name": "Lecture Notes in Geoinformation", "vol": "12", "iss": "3", "sp": "41", "ep": "58"},
    "language": {"code": "eng", "label": "English"},
    "publisher": "Springer",
}


class BuildOpenaireFieldsTest(TestCase):
    """Pure extraction from an OpenAIRE record (no DB / network)."""

    def test_extracts_longest_abstract(self):
        fields = build_openaire_fields(FAKE_RECORD)
        self.assertIn("abstract", fields)
        self.assertTrue(fields["abstract"].startswith("A considerably longer"))

    def test_keywords_drop_automated_schemes(self):
        fields = build_openaire_fields(FAKE_RECORD)
        # SDG and FOS classifications are dropped; free-text keywords kept.
        self.assertEqual(fields["keywords"], ["city models", "accuracy"])

    def test_authors_from_fullname_skip_blanks(self):
        fields = build_openaire_fields(FAKE_RECORD)
        self.assertEqual(fields["authors"], ["Gerald Gruber", "Christian Menard"])

    def test_extracts_container_pagination(self):
        fields = build_openaire_fields(FAKE_RECORD)
        self.assertEqual(fields["volume"], "12")
        self.assertEqual(fields["issue"], "3")
        self.assertEqual(fields["first_page"], "41")
        self.assertEqual(fields["last_page"], "58")

    def test_extracts_language_code_and_publisher(self):
        fields = build_openaire_fields(FAKE_RECORD)
        self.assertEqual(fields["language"], "eng")
        self.assertEqual(fields["publisher"], "Springer")

    def test_overlong_publisher_language_capped_to_field_length(self):
        record = {"publisher": "P" * 500, "language": {"code": "x" * 40}}
        fields = build_openaire_fields(record)
        self.assertEqual(len(fields["publisher"]), 255)
        self.assertEqual(len(fields["language"]), 8)

    def test_missing_container_language_publisher_omitted(self):
        record = {"descriptions": ["abstract"], "container": {}, "language": {}, "publisher": ""}
        fields = build_openaire_fields(record)
        for key in ("volume", "issue", "first_page", "last_page", "language", "publisher"):
            self.assertNotIn(key, fields)

    def test_empty_record_yields_no_candidates(self):
        self.assertEqual(build_openaire_fields(None), {})
        self.assertEqual(build_openaire_fields({"descriptions": [], "subjects": [], "authors": []}), {})

    def test_abstract_jats_markup_is_stripped(self):
        record = {
            "descriptions": [
                "<jats:p>We revise the chronology <jats:italic>substantially</jats:italic>, R&amp;D included.</jats:p>"
            ]
        }
        abstract = build_openaire_fields(record)["abstract"]
        self.assertNotIn("<jats:", abstract)
        self.assertNotIn("</jats:", abstract)
        self.assertEqual(abstract, "We revise the chronology substantially, R&D included.")


class ApplyEnrichmentTest(TestCase):
    """The fill-if-empty conflict policy + provenance recording."""

    def test_fills_empty_field_and_records_source(self):
        work = Work(title="t", abstract="")
        filled, offered = apply_enrichment(work, {"abstract": "From OpenAIRE"}, "openaire")
        self.assertEqual(work.abstract, "From OpenAIRE")
        self.assertEqual(filled, ["abstract"])
        self.assertEqual(offered, [])
        self.assertEqual(work.provenance["metadata_sources"]["abstract"], "openaire")

    def test_does_not_overwrite_existing_value(self):
        work = Work(title="t", abstract="Existing abstract")
        filled, offered = apply_enrichment(work, {"abstract": "From OpenAIRE"}, "openaire")
        self.assertEqual(work.abstract, "Existing abstract")
        self.assertEqual(filled, [])
        self.assertEqual(offered, ["abstract"])
        self.assertNotIn("abstract", work.provenance.get("metadata_sources", {}))

    def test_blank_candidate_is_skipped(self):
        work = Work(title="t", abstract="")
        filled, offered = apply_enrichment(work, {"abstract": "   "}, "openaire")
        self.assertEqual(filled, [])
        self.assertEqual(offered, [])
        self.assertEqual(work.abstract, "")


class EnrichWorkFromOpenaireTest(TestCase):
    def setUp(self):
        self.work = Work.objects.create(title="t", doi="10.1007/978-x", abstract="", status="h")

    @patch("works.harvesting.openaire.fetch_openaire_record", return_value=FAKE_RECORD)
    def test_match_fills_and_records_provenance(self, _mock):
        changed = enrich_work_from_openaire(self.work)
        self.work.refresh_from_db()
        self.assertTrue(changed)
        self.assertTrue(self.work.abstract.startswith("A considerably longer"))
        self.assertEqual(self.work.provenance["metadata_sources"]["abstract"], "openaire")
        self.assertEqual(self.work.provenance["openaire_match"]["status"], "matched")
        self.assertEqual(self.work.provenance["openaire_match"]["openaire_id"], "doi_dedup___::abc123")
        events = self.work.provenance["events"]
        self.assertEqual(events[-1]["type"], "openaire_enrich")
        self.assertIn("abstract", events[-1]["fields_filled"])

    @patch("works.harvesting.openaire.fetch_openaire_record", return_value=FAKE_RECORD)
    def test_match_fills_pagination_language_publisher(self, _mock):
        enrich_work_from_openaire(self.work)
        self.work.refresh_from_db()
        self.assertEqual(self.work.volume, "12")
        self.assertEqual(self.work.issue, "3")
        self.assertEqual(self.work.first_page, "41")
        self.assertEqual(self.work.last_page, "58")
        self.assertEqual(self.work.language, "eng")
        self.assertEqual(self.work.publisher, "Springer")
        filled = self.work.provenance["events"][-1]["fields_filled"]
        for key in ("volume", "issue", "first_page", "last_page", "language", "publisher"):
            self.assertIn(key, filled)
            self.assertEqual(self.work.provenance["metadata_sources"][key], "openaire")

    @patch("works.harvesting.openaire.fetch_openaire_record", return_value=FAKE_RECORD)
    def test_existing_publisher_not_overwritten(self, _mock):
        self.work.publisher = "Curator Press"
        self.work.save(update_fields=["publisher"])
        enrich_work_from_openaire(self.work)
        self.work.refresh_from_db()
        self.assertEqual(self.work.publisher, "Curator Press")
        event = self.work.provenance["events"][-1]
        self.assertIn("publisher", event["fields_offered_not_applied"])

    @patch("works.harvesting.openaire.fetch_openaire_record", return_value=FAKE_RECORD)
    def test_offered_not_applied_logged(self, _mock):
        self.work.abstract = "Curator-supplied abstract"
        self.work.save(update_fields=["abstract"])
        enrich_work_from_openaire(self.work)
        self.work.refresh_from_db()
        self.assertEqual(self.work.abstract, "Curator-supplied abstract")
        event = self.work.provenance["events"][-1]
        self.assertIn("abstract", event["fields_offered_not_applied"])
        self.assertNotIn("abstract", event.get("fields_filled") or [])

    @patch("works.harvesting.openaire.fetch_openaire_record", return_value=None)
    def test_no_match_records_status_none(self, _mock):
        changed = enrich_work_from_openaire(self.work)
        self.work.refresh_from_db()
        self.assertFalse(changed)
        self.assertEqual(self.work.provenance["openaire_match"]["status"], "none")
        self.assertIsNone(self.work.openaire_url)

    @patch("works.harvesting.openaire.fetch_openaire_record", return_value=FAKE_RECORD)
    def test_match_records_explore_url(self, _mock):
        enrich_work_from_openaire(self.work)
        self.work.refresh_from_db()
        expected = "https://explore.openaire.eu/search/result?id=doi_dedup___::abc123"
        self.assertEqual(self.work.provenance["openaire_match"]["url"], expected)
        # the model property mirrors the OpenAlex link for the landing page
        self.assertEqual(self.work.openaire_url, expected)


class CarefulUpdatePreservesEnrichedFieldsTest(TestCase):
    """A re-harvest that brings nothing must not wipe an enriched abstract."""

    def setUp(self):
        self.source = Source.objects.create(name="S", url_field="https://e.x/oai")
        self.event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

    def test_reharvest_keeps_nonempty_abstract(self):
        work = Work.objects.create(
            title="old", doi="10.1/keep", abstract="Enriched abstract", source=self.source, status="h"
        )
        new_fields = {"title": "new title", "abstract": None, "provenance": {"harvest": {}}}
        _carefully_update_work(work, new_fields, self.event)
        work.refresh_from_db()
        self.assertEqual(work.abstract, "Enriched abstract")
        self.assertEqual(work.title, "new title")


class EnrichEventSweepTest(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="S", url_field="https://e.x/oai")
        self.event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

    @patch("works.harvesting.openaire.fetch_openaire_record", return_value=FAKE_RECORD)
    def test_sweep_checks_all_doi_works_for_audit_trail(self, mock_fetch):
        missing = Work.objects.create(title="m", doi="10.1/m", abstract="", status="h", job=self.event)
        complete = Work.objects.create(
            title="c",
            doi="10.1/c",
            abstract="has one",
            authors=["A"],
            keywords=["k"],
            volume="1",
            issue="2",
            first_page="3",
            last_page="4",
            language="deu",
            publisher="Existing Press",
            status="h",
            job=self.event,
        )
        no_doi = Work.objects.create(title="n", abstract="", status="h", job=self.event)

        updated = enrich_event_from_openaire(self.event.id, throttle=0)

        missing.refresh_from_db()
        complete.refresh_from_db()
        # fill-if-empty: only the empty work is filled, so `updated` counts it alone
        self.assertTrue(missing.abstract.startswith("A considerably longer"))
        self.assertEqual(complete.abstract, "has one")
        self.assertEqual(updated, 1)
        # both DOI-bearing works are looked up (audit trail), the DOI-less one is skipped
        looked_up = {c.args[0] for c in mock_fetch.call_args_list}
        self.assertEqual(looked_up, {"10.1/m", "10.1/c"})
        self.assertNotIn(no_doi.doi, looked_up)
        # the already-complete work still records that OpenAIRE was checked
        self.assertEqual(complete.provenance["openaire_match"]["status"], "matched")
        self.assertIn("abstract", complete.provenance["events"][-1]["fields_offered_not_applied"])


class CompleteHarvestEnqueueTest(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="S", url_field="https://e.x/oai")
        self.event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

    @override_settings(OPTIMAP_OPENAIRE_ENRICH_ON_HARVEST=True)
    @patch("works.harvesting.common.fetch_and_store_crossref_works_count", return_value=None)
    @patch("works.harvesting.common.fetch_and_store_oai_works_count", return_value=None)
    @patch("works.harvesting.common.fetch_and_store_openalex_source_stats", return_value=None)
    @patch("works.harvesting.common.async_task")
    def test_enqueues_sweep_when_enabled(self, mock_async, *_stats):
        complete_harvest(self.event, HarvestStats(), HarvestWarningCollector())
        mock_async.assert_called_once()
        self.assertEqual(mock_async.call_args[0][0], "works.harvesting.openaire.enrich_event_from_openaire")
        self.assertEqual(mock_async.call_args[0][1], self.event.id)

    @override_settings(OPTIMAP_OPENAIRE_ENRICH_ON_HARVEST=False)
    @patch("works.harvesting.common.fetch_and_store_crossref_works_count", return_value=None)
    @patch("works.harvesting.common.fetch_and_store_oai_works_count", return_value=None)
    @patch("works.harvesting.common.fetch_and_store_openalex_source_stats", return_value=None)
    @patch("works.harvesting.common.async_task")
    def test_skips_sweep_when_disabled(self, mock_async, *_stats):
        complete_harvest(self.event, HarvestStats(), HarvestWarningCollector())
        mock_async.assert_not_called()


class EnrichOpenaireCommandTest(TestCase):
    def setUp(self):
        self.work = Work.objects.create(title="t", doi="10.1007/978-c", abstract="", status="h")

    @patch("works.harvesting.openaire.fetch_openaire_record", return_value=FAKE_RECORD)
    def test_command_fills_abstract(self, _mock):
        out = StringIO()
        call_command("enrich_openaire", "--throttle", "0", stdout=out)
        self.work.refresh_from_db()
        self.assertTrue(self.work.abstract.startswith("A considerably longer"))
        self.assertIn("Updated", out.getvalue())

    @patch("works.harvesting.openaire.fetch_openaire_record", return_value=FAKE_RECORD)
    def test_command_dry_run_writes_nothing(self, _mock):
        call_command("enrich_openaire", "--throttle", "0", "--dry-run", stdout=StringIO())
        self.work.refresh_from_db()
        self.assertEqual(self.work.abstract, "")

    @patch(
        "works.management.commands.enrich_openaire.async_task",
        return_value="9f8c2a1b4d5e6f708192a3b4c5d6e7f8",
    )
    @patch("works.harvesting.openaire.fetch_openaire_record", return_value=FAKE_RECORD)
    def test_async_enqueues_task_without_running(self, mock_fetch, mock_async):
        out = StringIO()
        call_command(
            "enrich_openaire",
            "--collection",
            "eartharxiv",
            "--async",
            stdout=out,
        )
        # --async must not run enrichment in-process.
        mock_fetch.assert_not_called()
        self.work.refresh_from_db()
        self.assertEqual(self.work.abstract, "")
        # It enqueues exactly one task to the queryset-based backfill function.
        mock_async.assert_called_once()
        self.assertEqual(mock_async.call_args[0][0], "works.tasks.enrich_openaire_backfill")
        self.assertEqual(mock_async.call_args[1]["collection"], "eartharxiv")
        # The async path runs in bounded chunks (re-enqueued continuations).
        self.assertTrue(mock_async.call_args[1]["chunked"])
        # Output carries both the raw id and the humanized admin "Name" so the
        # operator can locate the task in the Django Q admin.
        from django_q.humanhash import humanize

        output = out.getvalue()
        self.assertIn("9f8c2a1b4d5e6f708192a3b4c5d6e7f8", output)
        self.assertIn(humanize("9f8c2a1b4d5e6f708192a3b4c5d6e7f8"), output)


def _session_raising(status_code):
    """A fake requests session whose GET raises an HTTPError with ``status_code``."""
    resp = MagicMock()
    err = requests.HTTPError(f"{status_code} Client Error")
    err.response = MagicMock(status_code=status_code)
    resp.raise_for_status.side_effect = err
    session = MagicMock()
    session.get.return_value = resp
    return session


class FetchClassificationTest(TestCase):
    """fetch_openaire_record distinguishes systemic failures from 'not found'."""

    def test_systemic_status_raises_when_flag_set(self):
        for status in (401, 403, 408, 429, 500, 503):
            with self.subTest(status=status):
                with self.assertRaises(OpenAireUnavailable) as ctx:
                    fetch_openaire_record("10.1/x", session=_session_raising(status), raise_on_unavailable=True)
                self.assertEqual(ctx.exception.status, status)

    def test_404_returns_none_even_with_flag(self):
        # A genuine "not found" is not systemic — still None, never raises.
        self.assertIsNone(fetch_openaire_record("10.1/x", session=_session_raising(404), raise_on_unavailable=True))

    def test_403_returns_none_by_default(self):
        # Backward compatible: live per-work callers never see the exception.
        self.assertIsNone(fetch_openaire_record("10.1/x", session=_session_raising(403)))

    def test_connection_error_raises_with_null_status(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("boom")
        with self.assertRaises(OpenAireUnavailable) as ctx:
            fetch_openaire_record("10.1/x", session=session, raise_on_unavailable=True)
        self.assertIsNone(ctx.exception.status)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    OPTIMAP_OPENAIRE_ENRICH_ABORT_AFTER=2,
)
class SweepCircuitBreakerTest(TestCase):
    """A run of systemic failures aborts the sweep, emails staff, and fails the task."""

    def setUp(self):
        self.source = Source.objects.create(name="S", url_field="https://e.x/oai")
        self.event = HarvestingEvent.objects.create(source=self.source, status="in_progress")
        get_user_model().objects.create_user(
            username="curator", email="curator@example.org", is_staff=True, is_active=True
        )
        for i in range(5):
            Work.objects.create(title=f"w{i}", doi=f"10.1/{i}", status="h", job=self.event)

    @patch(
        "works.harvesting.openaire.fetch_openaire_record",
        side_effect=OpenAireUnavailable("403 Forbidden", status=403),
    )
    def test_aborts_emails_and_raises(self, _mock):
        with self.assertRaises(OpenAireUnavailable):
            enrich_event_from_openaire(self.event.id, throttle=0)
        # A staff alert naming the cause is sent (to every staff recipient).
        alerts = [m for m in mail.outbox if "OpenAIRE enrichment aborted" in m.subject]
        self.assertTrue(alerts)
        self.assertIn("curator@example.org", [addr for m in alerts for addr in m.to])
        self.assertIn("403", alerts[0].body)
        self.assertIn(settings.BASE_URL, alerts[0].body)


@override_settings(OPTIMAP_OPENAIRE_ENRICH_CHUNK_SECONDS=0)
class SweepChunkingTest(TestCase):
    """When the per-chunk budget is exhausted, the sweep re-enqueues a continuation."""

    def setUp(self):
        self.source = Source.objects.create(name="S", url_field="https://e.x/oai")
        self.event = HarvestingEvent.objects.create(source=self.source, status="in_progress")
        for i in range(3):
            Work.objects.create(title=f"w{i}", doi=f"10.1/{i}", abstract="", status="h", job=self.event)

    @patch("django_q.tasks.async_task")
    @patch("works.harvesting.openaire.fetch_openaire_record", return_value=FAKE_RECORD)
    def test_reenqueues_with_cursor_and_carry(self, _fetch, mock_async):
        enrich_event_from_openaire(self.event.id, throttle=0)
        # Budget 0 → one work processed, then a continuation is enqueued.
        mock_async.assert_called_once()
        self.assertEqual(mock_async.call_args[0][0], "works.harvesting.openaire.enrich_event_from_openaire")
        self.assertGreater(mock_async.call_args[1]["after_id"], 0)
        self.assertEqual(mock_async.call_args[1]["carry"]["processed"], 1)


@tag("online")
class OpenaireOnlineTest(TestCase):
    """Hits the real OpenAIRE Graph API; self-skips when unreachable."""

    def test_real_lookup_returns_abstract(self):
        record = fetch_openaire_record("10.1007/978-3-540-78946-8_4")
        if record is None:
            self.skipTest("OpenAIRE endpoint unreachable")
        fields = build_openaire_fields(record)
        self.assertTrue(fields.get("abstract"))
