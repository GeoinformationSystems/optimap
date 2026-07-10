# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests that HarvestingEvent.trigger_source is threaded correctly through every
harvest entry point and caller (issues #133, #44)."""

from unittest.mock import patch

import responses
from django.core.management import call_command
from django.test import TestCase

from works.harvesting.common import start_harvesting_event
from works.models import Collection, HarvestingEvent, Source


def _make_source(**overrides):
    defaults = dict(name="Trigger Source", url_field="http://example.org/oai", source_type="oai-pmh")
    defaults.update(overrides)
    return Source.objects.create(**defaults)


class StartHarvestingEventTriggerSourceTest(TestCase):
    def setUp(self):
        self.source = _make_source()

    def test_defaults_to_schedule_when_not_given(self):
        event = start_harvesting_event(self.source)
        self.assertEqual(event.trigger_source, "schedule")

    def test_uses_explicit_trigger_source(self):
        event = start_harvesting_event(self.source, trigger_source="admin-action")
        self.assertEqual(event.trigger_source, "admin-action")

    def test_reused_pending_event_keeps_its_own_trigger_source(self):
        pending = HarvestingEvent.objects.create(source=self.source, status="pending", trigger_source="ui")
        event = start_harvesting_event(self.source, event_id=pending.id, trigger_source="schedule")
        self.assertEqual(event.id, pending.id)
        self.assertEqual(event.trigger_source, "ui")

    def test_max_records_recorded_on_new_event(self):
        event = start_harvesting_event(self.source, max_records=20)
        self.assertEqual(event.max_records, 20)

    def test_max_records_none_for_unbounded_run(self):
        event = start_harvesting_event(self.source)
        self.assertIsNone(event.max_records)

    def test_max_records_recorded_on_reused_pending_event(self):
        pending = HarvestingEvent.objects.create(source=self.source, status="pending", trigger_source="ui")
        event = start_harvesting_event(self.source, event_id=pending.id, max_records=5)
        self.assertEqual(event.id, pending.id)
        self.assertEqual(event.max_records, 5)
        pending.refresh_from_db()
        self.assertEqual(pending.max_records, 5)


class HarvestOaiEndpointTriggerSourceTest(TestCase):
    @responses.activate
    def test_trigger_source_persisted_on_event(self):
        from works.harvesting.oai import harvest_oai_endpoint

        source = _make_source(url_field="http://example.org/oai-trigger")
        responses.add(
            responses.GET,
            "http://example.org/oai-trigger",
            body=(
                b'<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords/></OAI-PMH>'
            ),
            content_type="text/xml",
        )
        harvest_oai_endpoint(source.id, trigger_source="management-command")
        event = HarvestingEvent.objects.filter(source=source).latest("started_at")
        self.assertEqual(event.trigger_source, "management-command")


class HarvestSourcesCommandTriggerSourceTest(TestCase):
    """The harvest_sources management command must tag its events management-command."""

    @responses.activate
    def test_sync_path_passes_management_command_trigger_source(self):
        # Real (empty) OAI-PMH response — exercises the actual command dispatch
        # + harvester + start_harvesting_event chain instead of mocking the
        # harvester away (which would leave the command's own post-run
        # `HarvestingEvent.objects.filter(...).latest(...)` lookup with nothing
        # to find).
        collection = Collection.objects.create(identifier="geo-leo-test", name="GEO-LEO test")
        Source.objects.create(
            name="GEO-LEO e-docs",
            url_field="https://e-docs.geo-leo.de/server/oai/request?verb=ListRecords&metadataPrefix=oai_dc",
            source_type="oai-pmh",
            collection=collection,
        )
        empty_oai_xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords/></OAI-PMH>'
        )
        responses.add(
            responses.GET,
            "https://e-docs.geo-leo.de/server/oai/request",
            body=empty_oai_xml,
            content_type="text/xml",
        )
        call_command("harvest_sources", source=["geo-leo"])
        event = HarvestingEvent.objects.filter(source__name="GEO-LEO e-docs").latest("started_at")
        self.assertEqual(event.trigger_source, "management-command")

    @patch("works.management.commands.harvest_sources.async_task")
    def test_async_path_passes_management_command_trigger_source(self, mock_async):
        mock_async.return_value = "fake-task-id"
        collection = Collection.objects.create(identifier="geo-leo-test-async", name="GEO-LEO test async")
        Source.objects.create(
            name="GEO-LEO e-docs",
            url_field="https://e-docs.geo-leo.de/server/oai/request?verb=ListRecords&metadataPrefix=oai_dc",
            source_type="oai-pmh",
            collection=collection,
        )
        call_command("harvest_sources", source=["geo-leo"], async_mode=True)
        mock_async.assert_called_once()
        args, kwargs = mock_async.call_args
        self.assertEqual(args[0], "works.tasks.harvest_oai_endpoint")
        # No trigger_source kwarg on the task call — the pre-created event
        # (asserted below) already carries it via event_id.
        self.assertNotIn("trigger_source", kwargs)
        event = HarvestingEvent.objects.get(id=kwargs["event_id"])
        self.assertEqual(event.trigger_source, "management-command")


class SourceLastHarvestAnnotationTest(TestCase):
    def test_annotation_matches_latest_event_and_does_not_duplicate_rows(self):
        from django.db.models import Max, Q

        collection = Collection.objects.create(identifier="last-harvest-test", name="Last Harvest Test")
        source = _make_source(name="Annotated Source", collection=collection)
        HarvestingEvent.objects.create(source=source, status="completed")
        latest = HarvestingEvent.objects.create(source=source, status="completed")

        annotated = list(
            Source.objects.filter(Q(collection=collection))
            .distinct()
            .annotate(last_harvest_at=Max("harvesting_events__started_at"))
        )
        self.assertEqual(len(annotated), 1)
        self.assertEqual(annotated[0].last_harvest_at, latest.started_at)
