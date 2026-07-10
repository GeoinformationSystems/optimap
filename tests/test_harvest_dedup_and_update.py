# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Per-source dedup + careful-update behaviour shared by all four harvesters.

Exercised via the MaRESS harvester (it's the cleanest path to mock end-to-end
since we don't depend on OAI-PMH XML or Crossref API JSON), but the behaviour
is implemented in ``_save_or_update_work`` and ``_carefully_update_work`` and
applies to OAI-PMH, RSS, Crossref, and MaRESS identically.
"""

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.gis.geos import GeometryCollection, Point
from django.test import TestCase

from works.models import HarvestingEvent, Source, Work

SAMPLE_JSON = Path(__file__).resolve().parent / "harvesting" / "mountain_wetlands" / "items_sample.json"


def _mock_response(payload):
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = payload
    resp.headers = {"Content-Type": "application/json"}
    return resp


def _patched_session(payload):
    session = MagicMock()
    session.get.return_value = _mock_response(payload)
    return patch("works.harvesting.mountain_wetlands._mwr_session", return_value=session)


def _no_op_openalex():
    return patch("works.harvesting.mountain_wetlands.build_openalex_fields", return_value=({}, {}))


class SameSourceDedupTests(TestCase):
    """Without --update, same-source duplicates are silently skipped."""

    @classmethod
    def setUpTestData(cls):
        cls.payload = json.loads(SAMPLE_JSON.read_text())

    def setUp(self):
        self.source = Source.objects.create(
            name="MaRESS",
            source_type="mountain-wetlands",
            url_field="https://andes.mountain-wetlands-repository.info/api/v1/items/",
        )

    def test_re_run_does_not_create_duplicates_or_updates(self):
        from works.tasks import harvest_mountain_wetlands

        with _patched_session(self.payload), _no_op_openalex():
            harvest_mountain_wetlands(self.source.id)
            self.assertEqual(Work.objects.count(), 3)

            # Re-run: payload unchanged, no --update.
            saved, processed = harvest_mountain_wetlands(self.source.id)
            self.assertEqual(saved, 0)
            self.assertEqual(processed, 3)
            self.assertEqual(Work.objects.count(), 3)

    def test_skipped_dup_keeps_geometry_intact(self):
        from works.tasks import harvest_mountain_wetlands

        with _patched_session(self.payload), _no_op_openalex():
            harvest_mountain_wetlands(self.source.id)
        baied = Work.objects.get(title__startswith="Evolution of High Andean")
        original_geom_wkt = baied.geometry.wkt

        with _patched_session(self.payload), _no_op_openalex():
            harvest_mountain_wetlands(self.source.id)
        baied.refresh_from_db()
        self.assertEqual(baied.geometry.wkt, original_geom_wkt)


class CarefulUpdateTests(TestCase):
    """With --update, same-source duplicates are updated in place — but
    geometry, temporal metadata, and curation state are preserved."""

    @classmethod
    def setUpTestData(cls):
        cls.payload = json.loads(SAMPLE_JSON.read_text())

    def setUp(self):
        self.source = Source.objects.create(
            name="MaRESS",
            source_type="mountain-wetlands",
            url_field="https://andes.mountain-wetlands-repository.info/api/v1/items/",
        )
        # Seed: harvest once.
        from works.tasks import harvest_mountain_wetlands

        with _patched_session(self.payload), _no_op_openalex():
            harvest_mountain_wetlands(self.source.id)
        self.no_sites = Work.objects.get(title="Record Without Study Sites")
        self.baied = Work.objects.get(title__startswith="Evolution of High Andean")

    def _payload_without_study_sites(self):
        """Modified payload: drop study_sites for the Baied record so the new
        harvest brings empty geometry. Tests that the existing geometry is
        preserved instead of being wiped."""
        modified = copy.deepcopy(self.payload)
        for item in modified["data"]:
            if item["title"].startswith("Evolution of High Andean"):
                item["study_sites"] = []
        return modified

    def test_update_existing_replaces_title_and_abstract(self):
        from works.tasks import harvest_mountain_wetlands

        # Modify the upstream title and abstract.
        modified = copy.deepcopy(self.payload)
        for item in modified["data"]:
            if item["title"].startswith("Evolution of High Andean"):
                item["title"] = "Evolution of High Andean Puna — REVISED TITLE"
                item["abstractNote"] = "Substantially revised abstract."

        with _patched_session(modified), _no_op_openalex():
            harvest_mountain_wetlands(self.source.id, update_existing=True)

        self.baied.refresh_from_db()
        self.assertEqual(self.baied.title, "Evolution of High Andean Puna — REVISED TITLE")
        self.assertEqual(self.baied.abstract, "Substantially revised abstract.")

    def test_update_preserves_geometry_when_new_is_empty(self):
        from works.tasks import harvest_mountain_wetlands

        original_geom_wkt = self.baied.geometry.wkt
        self.assertNotEqual(
            original_geom_wkt, "GEOMETRYCOLLECTION EMPTY", "precondition: seed work must have a non-empty geometry"
        )

        with _patched_session(self._payload_without_study_sites()), _no_op_openalex():
            harvest_mountain_wetlands(self.source.id, update_existing=True)

        self.baied.refresh_from_db()
        self.assertEqual(
            self.baied.geometry.wkt,
            original_geom_wkt,
            "existing geometry must survive a re-harvest with empty study_sites",
        )

    def test_update_preserves_curator_added_geometry(self):
        """Simulate a curator adding geometry to a record the source never gave us."""
        from works.tasks import harvest_mountain_wetlands

        # The "no_sites" record was harvested with empty geometry. Pretend a
        # curator contributed geometry through OPTIMAP afterwards.
        self.no_sites.geometry = GeometryCollection(Point(0, 0, srid=4326), srid=4326)
        self.no_sites.save(update_fields=["geometry"])

        with _patched_session(self.payload), _no_op_openalex():
            harvest_mountain_wetlands(self.source.id, update_existing=True)

        self.no_sites.refresh_from_db()
        self.assertFalse(self.no_sites.geometry.empty, "curator-contributed geometry must NOT be wiped by re-harvest")
        self.assertEqual(self.no_sites.geometry.num_geom, 1)

    def test_update_preserves_user_published_status(self):
        """A Work the admin has marked Published must not flip back to Harvested."""
        from works.tasks import harvest_mountain_wetlands

        self.baied.status = "p"
        self.baied.save(update_fields=["status"])

        with _patched_session(self.payload), _no_op_openalex():
            harvest_mountain_wetlands(self.source.id, update_existing=True)

        self.baied.refresh_from_db()
        self.assertEqual(self.baied.status, "p", "status must survive re-harvest")

    def test_update_appends_event_to_provenance(self):
        from works.tasks import harvest_mountain_wetlands

        # Pretend a curator contributed geometry, leaving an event behind.
        self.baied.provenance.setdefault("events", []).append(
            {"type": "contribution", "user_id": 1, "at": "2026-01-01T00:00:00"},
        )
        self.baied.save(update_fields=["provenance"])

        with _patched_session(self.payload), _no_op_openalex():
            harvest_mountain_wetlands(self.source.id, update_existing=True)

        self.baied.refresh_from_db()
        events = self.baied.provenance.get("events", [])
        # Original contribution event is still there.
        self.assertTrue(any(e["type"] == "contribution" for e in events))
        # And the harvest_update event was appended.
        self.assertTrue(any(e["type"] == "harvest_update" for e in events))


class CrossSourceDedupTests(TestCase):
    """A Work that already exists under a *different* Source is never
    auto-merged — even with --update — and the second source's harvest is
    skipped with a log message instead of crashing on Work.url uniqueness."""

    @classmethod
    def setUpTestData(cls):
        cls.payload = json.loads(SAMPLE_JSON.read_text())

    def setUp(self):
        self.source_a = Source.objects.create(
            name="MaRESS A",
            source_type="mountain-wetlands",
            url_field="https://andes.mountain-wetlands-repository.info/api/v1/items/",
        )
        # Source B uses the same base URL — so generated item URLs collide.
        self.source_b = Source.objects.create(
            name="MaRESS B",
            source_type="mountain-wetlands",
            url_field="https://andes.mountain-wetlands-repository.info/api/v1/items/",
        )

    def test_cross_source_collision_skipped_not_crashed(self):
        from works.tasks import harvest_mountain_wetlands

        # Source A harvests first.
        with _patched_session(self.payload), _no_op_openalex():
            harvest_mountain_wetlands(self.source_a.id)
        self.assertEqual(Work.objects.filter(source=self.source_a).count(), 3)
        self.assertEqual(Work.objects.filter(source=self.source_b).count(), 0)

        # Source B sees the same item URLs — must not crash, must not steal.
        with _patched_session(self.payload), _no_op_openalex():
            saved_b, processed_b = harvest_mountain_wetlands(self.source_b.id)
        self.assertEqual(saved_b, 0, "cross-source duplicates must not be saved as new works")
        # Source A's works untouched, Source B owns nothing.
        self.assertEqual(Work.objects.filter(source=self.source_a).count(), 3)
        self.assertEqual(Work.objects.filter(source=self.source_b).count(), 0)

    def test_cross_source_collision_skipped_even_with_update_flag(self):
        from works.tasks import harvest_mountain_wetlands

        with _patched_session(self.payload), _no_op_openalex():
            harvest_mountain_wetlands(self.source_a.id)

        # --update on source B must NOT silently re-parent A's works to B.
        with _patched_session(self.payload), _no_op_openalex():
            harvest_mountain_wetlands(self.source_b.id, update_existing=True)

        for w in Work.objects.all():
            self.assertEqual(
                w.source_id, self.source_a.id, "cross-source dup must not flip ownership even with --update"
            )


class SaveOrUpdateHelperTests(TestCase):
    """Direct unit tests on _save_or_update_work without going through the
    full harvester."""

    def setUp(self):
        self.source = Source.objects.create(
            name="S",
            source_type="oai-pmh",
            url_field="https://example.com/oai",
        )

    def _kwargs(self, **overrides):
        from works.tasks import get_or_create_admin_command_user

        kwargs = dict(
            title="X",
            abstract="a",
            url="https://example.com/x",
            doi="10.1234/x",
            source=self.source,
            status="h",
            geometry=GeometryCollection(Point(1, 2, srid=4326), srid=4326),
            timeperiod_startdate=["2024"],
            timeperiod_enddate=["2024"],
            provenance={"harvest": {"harvester": "test"}},
            created_by=get_or_create_admin_command_user(),
        )
        kwargs.update(overrides)
        return kwargs

    def test_first_save_creates(self):
        from works.tasks import _save_or_update_work

        work, action = _save_or_update_work(self._kwargs(), self.source, None)
        self.assertEqual(action, "created")
        self.assertIsNotNone(work.pk)

    def test_second_save_skipped_without_update(self):
        from works.tasks import _save_or_update_work

        _save_or_update_work(self._kwargs(), self.source, None)
        _, action = _save_or_update_work(self._kwargs(), self.source, None)
        self.assertEqual(action, "skipped_same_source")
        self.assertEqual(Work.objects.count(), 1)

    def test_second_save_updates_with_flag(self):
        from works.tasks import _save_or_update_work

        _save_or_update_work(self._kwargs(), self.source, None)
        _, action = _save_or_update_work(
            self._kwargs(abstract="new abstract"),
            self.source,
            None,
            update_existing=True,
        )
        self.assertEqual(action, "updated")
        self.assertEqual(Work.objects.count(), 1)
        self.assertEqual(Work.objects.get().abstract, "new abstract")

    def test_cross_source_returns_skipped_cross_source(self):
        from works.tasks import _save_or_update_work

        other_source = Source.objects.create(
            name="Other",
            source_type="oai-pmh",
            url_field="https://other.example.com/oai",
        )
        _save_or_update_work(self._kwargs(), self.source, None)
        _, action = _save_or_update_work(
            self._kwargs(source=other_source),
            other_source,
            None,
            update_existing=True,
        )
        self.assertEqual(action, "skipped_cross_source")
        self.assertEqual(Work.objects.count(), 1)
        self.assertEqual(Work.objects.get().source_id, self.source.id)

    def test_author_records_is_not_a_real_work_field_and_is_stripped(self):
        # Regression: build_openalex_fields() propagates author_records (for
        # link_openalex_authors() to read after the save), and several
        # harvesters build work_kwargs via dict(..., **openalex_fields) — so
        # author_records ends up in work_kwargs too. Work has no such field;
        # Work.objects.create(**work_kwargs) must not choke on it.
        from works.tasks import _save_or_update_work

        records = [{"name": "A. Author", "orcid": "0000-0001-2345-6789", "openalex_id": "A1", "rank": 0}]
        work, action = _save_or_update_work(self._kwargs(author_records=records), self.source, None)
        self.assertEqual(action, "created")
        self.assertIsNotNone(work.pk)

    def test_author_records_not_mutated_out_of_callers_dict(self):
        # link_openalex_authors(work, kwargs) is called by several harvesters
        # right after _save_or_update_work(kwargs, ...) using the *same* dict
        # — stripping author_records must not mutate the caller's copy.
        from works.tasks import _save_or_update_work

        records = [{"name": "A. Author", "orcid": "0000-0001-2345-6789", "openalex_id": "A1", "rank": 0}]
        kwargs = self._kwargs(author_records=records)
        _save_or_update_work(kwargs, self.source, None)
        self.assertEqual(kwargs["author_records"], records)

    def test_author_records_stripped_on_update_path_too(self):
        from works.tasks import _save_or_update_work

        _save_or_update_work(self._kwargs(), self.source, None)
        records = [{"name": "A. Author", "orcid": "0000-0001-2345-6789", "openalex_id": "A1", "rank": 0}]
        work, action = _save_or_update_work(
            self._kwargs(author_records=records, abstract="updated"),
            self.source,
            None,
            update_existing=True,
        )
        self.assertEqual(action, "updated")
        self.assertEqual(work.abstract, "updated")


class EmptyDoiBackfillTests(TestCase):
    """Targeted DOI backfill: when a re-harvest delivers a DOI for an
    existing record that has none, write just the DOI even if the source
    differs or ``update_existing`` is False. This is what closes the gap
    on AGILE-GISS works that were ingested earlier via Copernicus OAI-PMH
    without DOIs in ``dc:identifier`` and stayed empty across re-harvests.
    """

    def setUp(self):
        self.source_a = Source.objects.create(
            name="A",
            source_type="oai-pmh",
            url_field="https://a.example.com/oai",
        )
        self.source_b = Source.objects.create(
            name="B",
            source_type="openalex",
            url_field="https://b.example.com/oa",
            openalex_id="S4210203054",
        )

    def _kwargs(self, source, **overrides):
        from works.tasks import get_or_create_admin_command_user

        kwargs = dict(
            title="X",
            abstract="a",
            url="https://landing.example.com/x",
            source=source,
            status="h",
            geometry=GeometryCollection(),
            timeperiod_startdate=[],
            timeperiod_enddate=[],
            provenance={"harvest": {"harvester": "test"}},
            created_by=get_or_create_admin_command_user(),
        )
        kwargs.update(overrides)
        return kwargs

    def test_cross_source_reharvest_backfills_empty_doi(self):
        from works.tasks import _save_or_update_work

        # Step 1: legacy harvest stores work with no DOI but a landing URL.
        first, _ = _save_or_update_work(
            self._kwargs(self.source_a, doi=None),
            self.source_a,
            None,
        )
        self.assertIsNone(first.doi)

        # Step 2: re-harvest from a different source with the same URL but a
        # DOI now available. Without the backfill helper this would return
        # ``skipped_cross_source`` and leave the DOI empty.
        event = HarvestingEvent.objects.create(source=self.source_b, status="in_progress")
        same, action = _save_or_update_work(
            self._kwargs(self.source_b, doi="10.5194/agile-giss-1-1-2020"),
            self.source_b,
            event,
        )
        self.assertEqual(action, "doi_backfilled")
        self.assertEqual(same.pk, first.pk)
        same.refresh_from_db()
        self.assertEqual(same.doi, "10.5194/agile-giss-1-1-2020")
        # Source row stays attached to the legacy harvester — backfill is
        # DOI-only and never reassigns ownership.
        self.assertEqual(same.source_id, self.source_a.id)
        # Provenance grew an audit entry that names the backfill action.
        events = (same.provenance or {}).get("events") or []
        self.assertTrue(any(e.get("type") == "doi_backfill" for e in events))

    def test_same_source_reharvest_backfills_empty_doi_without_update_flag(self):
        from works.tasks import _save_or_update_work

        first, _ = _save_or_update_work(
            self._kwargs(self.source_a, doi=None),
            self.source_a,
            None,
        )
        self.assertIsNone(first.doi)

        event = HarvestingEvent.objects.create(source=self.source_a, status="in_progress")
        # No update_existing=True — same-source path would normally skip.
        same, action = _save_or_update_work(
            self._kwargs(self.source_a, doi="10.5194/agile-giss-1-1-2020"),
            self.source_a,
            event,
        )
        self.assertEqual(action, "doi_backfilled")
        same.refresh_from_db()
        self.assertEqual(same.doi, "10.5194/agile-giss-1-1-2020")

    def test_existing_doi_is_preserved_when_new_kwargs_have_no_doi(self):
        from works.tasks import _save_or_update_work

        first, _ = _save_or_update_work(
            self._kwargs(self.source_a, doi="10.5194/keep-me"),
            self.source_a,
            None,
        )

        # New harvest comes in with no DOI — must NOT clear the existing one.
        # This dedup path matches by URL because the new kwargs has no DOI.
        same, action = _save_or_update_work(
            self._kwargs(self.source_a, doi=None),
            self.source_a,
            None,
        )
        self.assertNotEqual(action, "doi_backfilled")
        same.refresh_from_db()
        self.assertEqual(same.doi, "10.5194/keep-me")
