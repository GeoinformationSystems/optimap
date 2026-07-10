# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for ``works.harvesting.common.start_harvesting_event``.

The helper lets the ``--async`` route of ``harvest_sources`` pre-create a
``pending`` HarvestingEvent (so its PK can be printed and matched in the Django
admin) and have the running task reuse it instead of creating a fresh row.
"""

from django.test import TestCase

from works.harvesting.common import start_harvesting_event
from works.models import HarvestingEvent, Source


class StartHarvestingEventTest(TestCase):
    def setUp(self):
        self.source = Source.objects.create(
            name="Test Source",
            url_field="https://example.org/oai",
            source_type="oai-pmh",
        )

    def test_creates_event_when_no_event_id(self):
        event = start_harvesting_event(self.source)
        self.assertEqual(event.source, self.source)
        self.assertEqual(event.status, "in_progress")

    def test_reuses_pending_event_and_marks_in_progress(self):
        pending = HarvestingEvent.objects.create(source=self.source, status="pending")
        event = start_harvesting_event(self.source, pending.id)
        self.assertEqual(event.id, pending.id)
        self.assertEqual(event.status, "in_progress")
        self.assertEqual(HarvestingEvent.objects.count(), 1)

    def test_creates_new_event_when_event_belongs_to_other_source(self):
        other = Source.objects.create(
            name="Other",
            url_field="https://other.example/oai",
            source_type="oai-pmh",
        )
        foreign = HarvestingEvent.objects.create(source=other, status="pending")
        event = start_harvesting_event(self.source, foreign.id)
        self.assertNotEqual(event.id, foreign.id)
        self.assertEqual(event.source, self.source)

    def test_refuses_to_start_when_pre_created_already_completed(self):
        # Regression: this used to silently create a *second* event (mislabeled
        # with the model's "schedule" trigger_source default, since a retry
        # never passes one) — which is exactly what happens when Django-Q
        # retries an already-concluded task after its event has gone terminal.
        # Never resurrect a finished event under a fresh, misleading identity;
        # refuse instead so the caller aborts cleanly.
        done = HarvestingEvent.objects.create(source=self.source, status="completed")
        event = start_harvesting_event(self.source, done.id)
        self.assertIsNone(event)

    def test_refuses_to_start_when_pre_created_already_failed(self):
        done = HarvestingEvent.objects.create(source=self.source, status="failed")
        event = start_harvesting_event(self.source, done.id)
        self.assertIsNone(event)

    def test_refuses_to_start_when_pre_created_already_cancelled(self):
        done = HarvestingEvent.objects.create(source=self.source, status="cancelled")
        event = start_harvesting_event(self.source, done.id)
        self.assertIsNone(event)

    def test_creates_new_event_when_event_id_missing(self):
        event = start_harvesting_event(self.source, 999999)
        self.assertEqual(event.source, self.source)
        self.assertEqual(event.status, "in_progress")
