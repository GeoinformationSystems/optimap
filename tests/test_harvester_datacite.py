# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the DataCite IGSN physical-sample harvester (#187)."""

import responses
from django.core import mail
from django.test import TestCase, override_settings, tag

from works.harvesting.datacite import (
    _build_datacite_query,
    _geometry_from_geolocations,
    harvest_datacite,
    parse_datacite_response_and_save_works,
)
from works.harvesting.sessions import DATACITE_API_URL
from works.models import Source, Work, WorkRelation


def _record(
    doi,
    lat=None,
    lon=None,
    box=None,
    related=None,
    collected=None,
    publisher="SESAR",
    resource_type=None,
    subjects=None,
    contributors=None,
    place=None,
    descriptions=None,
):
    attrs = {
        "doi": doi,
        "publisher": publisher,
        "titles": [{"title": f"Sample {doi}"}],
        "creators": [{"name": "Doe, Jane"}],
        "url": f"https://app.geosamples.org/sample/igsn/{doi}",
        "geoLocations": [],
        "relatedIdentifiers": related or [],
        "dates": [],
        "types": {"resourceTypeGeneral": "PhysicalObject", "resourceType": resource_type},
        "subjects": [{"subject": s} for s in (subjects or [])],
        "contributors": [{"name": c} for c in (contributors or [])],
        "descriptions": [{"description": d, "descriptionType": "Abstract"} for d in (descriptions or [])],
    }
    if lat is not None:
        point = {"geoLocationPoint": {"pointLatitude": lat, "pointLongitude": lon}}
        if place:
            point["geoLocationPlace"] = place
        attrs["geoLocations"].append(point)
    if box is not None:
        attrs["geoLocations"].append({"geoLocationBox": box})
    if collected:
        attrs["dates"].append({"date": collected, "dateType": "Collected"})
    return {"id": doi, "type": "dois", "attributes": attrs}


def _response(records, next_url=None):
    body = {"data": records, "links": {}}
    if next_url:
        body["links"]["next"] = next_url
    return body


class DataCiteQueryTest(TestCase):
    def test_default_query_requires_coordinates(self):
        q = _build_datacite_query({"publisher": "SESAR", "require_coordinates": True})
        self.assertIn("types.resourceTypeGeneral:PhysicalObject", q)
        self.assertIn('publisher:"SESAR"', q)
        self.assertIn("_exists_:geoLocations.geoLocationPoint", q)

    def test_bbox_and_related_doi_filters(self):
        q = _build_datacite_query({"only_with_related_doi": True, "bbox": [-80, 0, -70, 10]})
        self.assertIn("relatedIdentifiers.relatedIdentifierType:DOI", q)
        self.assertIn("pointLatitude:[0 TO 10]", q)
        self.assertIn("pointLongitude:[-80 TO -70]", q)


class GeometryParsingTest(TestCase):
    def test_point(self):
        geom = _geometry_from_geolocations([{"geoLocationPoint": {"pointLatitude": 37.05, "pointLongitude": -119.25}}])
        self.assertIsNotNone(geom)
        self.assertEqual(geom.srid, 4326)
        self.assertAlmostEqual(geom[0].x, -119.25)

    def test_box(self):
        geom = _geometry_from_geolocations(
            [
                {
                    "geoLocationBox": {
                        "westBoundLongitude": 1,
                        "eastBoundLongitude": 2,
                        "southBoundLatitude": 3,
                        "northBoundLatitude": 4,
                    }
                }
            ]
        )
        self.assertIsNotNone(geom)
        self.assertEqual(geom[0].geom_type, "Polygon")

    def test_no_geometry(self):
        self.assertIsNone(_geometry_from_geolocations([{"geoLocationPlace": "somewhere"}]))
        self.assertIsNone(_geometry_from_geolocations([]))


class DataCiteHarvestTest(TestCase):
    def setUp(self):
        from works.harvesting.common import ensure_collection_for_source

        self.source = Source.objects.create(
            name="SESAR test",
            url_field="https://api.datacite.org/dois",
            source_type="datacite",
        )
        ensure_collection_for_source(self.source)

    def _parse(self, records, **kwargs):
        from works.harvesting.common import HarvestStats
        from works.models import HarvestingEvent

        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")
        responses.add(responses.GET, DATACITE_API_URL, json=_response(records), status=200)
        config = {"publisher": "SESAR", "require_coordinates": True, "sort": "created"}
        stats = HarvestStats()
        return parse_datacite_response_and_save_works(self.source, event, config, stats=stats, **kwargs), stats

    @responses.activate
    def test_creates_sample_works_and_skips_no_geo(self):
        records = [
            _record("10.58052/aaaa1111", lat=37.05, lon=-119.25, collected="2024-05-01"),
            _record(
                "10.58052/bbbb2222",
                box={
                    "westBoundLongitude": 1,
                    "eastBoundLongitude": 2,
                    "southBoundLatitude": 3,
                    "northBoundLatitude": 4,
                },
            ),
            _record("10.58052/cccc3333"),  # no geometry -> skipped
        ]
        (saved, seen), stats = self._parse(records)

        self.assertEqual(seen, 3)
        self.assertEqual(saved, 2)
        works = Work.objects.filter(type="physical-object")
        self.assertEqual(works.count(), 2)
        w = Work.objects.get(doi="10.58052/aaaa1111")
        self.assertEqual(w.status, "p")
        self.assertIsNotNone(w.geometry)
        self.assertEqual(w.timeperiod_startdate, ["2024-05-01"])
        self.assertEqual(w.provenance["metadata_sources"]["geometry"], "datacite.geoLocations")
        # Auto-tagged into the source collection.
        self.assertTrue(w.collections.exists())

    @responses.activate
    def test_creates_work_relations_from_related_identifiers(self):
        records = [
            _record(
                "10.58052/dddd4444",
                lat=2.52,
                lon=-72.64,
                related=[
                    {
                        "relationType": "IsReferencedBy",
                        "relatedIdentifier": "https://doi.org/10.15485/3374642",
                        "relatedIdentifierType": "DOI",
                    },
                    {
                        "relationType": "IsReferencedBy",
                        "relatedIdentifier": "https://whondrs.pnnl.gov",
                        "relatedIdentifierType": "URL",
                    },
                ],
            ),
        ]
        self._parse(records)
        sample = Work.objects.get(doi="10.58052/dddd4444")
        rels = WorkRelation.objects.filter(from_work=sample)
        self.assertEqual(rels.count(), 2)
        doi_rel = rels.get(to_identifier_type="DOI")
        # DOI normalized to bare form; dangling until the paper is harvested.
        self.assertEqual(doi_rel.to_identifier, "10.15485/3374642")
        self.assertEqual(doi_rel.relation_type, "is_referenced_by")
        self.assertIsNone(doi_rel.to_work_id)

    @responses.activate
    def test_dedup_on_rerun(self):
        records = [_record("10.58052/eeee5555", lat=1, lon=1)]
        self._parse(records)
        (saved, seen), stats = self._parse(records)
        self.assertEqual(saved, 0)
        self.assertEqual(stats.skipped_same_source, 1)
        self.assertEqual(Work.objects.filter(doi="10.58052/eeee5555").count(), 1)

    @responses.activate
    def test_max_records_cap(self):
        records = [_record(f"10.58052/cap{i}", lat=i, lon=i) for i in range(5)]
        (saved, seen), stats = self._parse(records, max_records=2)
        self.assertEqual(seen, 2)
        self.assertEqual(saved, 2)

    @responses.activate
    def test_synthesises_description_and_captures_fields(self):
        records = [
            _record(
                "10.58052/meta0001",
                lat=35.96,
                lon=-84.28,
                collected="2017-05-15",
                resource_type="Specimen",
                subjects=["Specimen", "Rock", "Macrobiology&gt;Biology"],
                contributors=["NEON Mosquito Collection"],
                place="Oak Ridge NEON (ORNL), Tennessee, United States",
            )
        ]
        self._parse(records)
        w = Work.objects.get(doi="10.58052/meta0001")
        # Synthesised abstract mentions kind, material, date, place, registrant, repository.
        self.assertIn("Physical sample (Specimen)", w.abstract)
        self.assertIn("Rock", w.abstract)
        self.assertIn("2017-05-15", w.abstract)
        self.assertIn("Oak Ridge NEON", w.abstract)
        self.assertIn("Registered by SESAR", w.abstract)
        self.assertIn("NEON Mosquito Collection", w.abstract)
        # subjects → keywords, HTML entity unescaped, resource-type duplicate kept in keywords.
        self.assertIn("Rock", w.keywords)
        self.assertIn("Macrobiology>Biology", w.keywords)
        # geoLocationPlace → placename; structured sample provenance recorded.
        self.assertEqual(w.placename, "Oak Ridge NEON (ORNL), Tennessee, United States")
        ms = w.provenance["metadata_sources"]
        self.assertEqual(ms["abstract"], "synthesised")
        self.assertEqual(ms["keywords"], "datacite.subjects")
        self.assertEqual(ms["placename"], "datacite.geoLocationPlace")
        self.assertEqual(ms["title"], "datacite.title")
        self.assertEqual(ms["authors"], "datacite.creators")
        # Synthesised-description provenance is self-documenting.
        sample = w.provenance["sample"]
        self.assertEqual(sample["resource_type"], "Specimen")
        self.assertEqual(sample["repositories"], ["NEON Mosquito Collection"])
        desc = sample["description"]
        self.assertTrue(desc["synthesised"])
        self.assertFalse(desc["source_description_appended"])
        # Records exactly which fields fed the sentence.
        for field in ("resource_type", "subjects", "collected_date", "geoLocationPlace", "publisher", "contributors"):
            self.assertIn(field, desc["synthesised_from"])

    @responses.activate
    def test_appends_real_description_as_paragraphs(self):
        records = [
            _record(
                "10.58052/meta0002",
                lat=1,
                lon=1,
                resource_type="Core",
                descriptions=["Collected during cruise XY42. Stored frozen at -20C."],
            )
        ]
        self._parse(records)
        w = Work.objects.get(doi="10.58052/meta0002")
        # Synthesised sentence first, then the real description as a new paragraph.
        self.assertTrue(w.abstract.startswith("Physical sample (Core)"))
        self.assertIn("\n\nCollected during cruise XY42.", w.abstract)
        self.assertEqual(w.provenance["metadata_sources"]["abstract"], "synthesised+datacite.descriptions")
        self.assertTrue(w.provenance["sample"]["description"]["source_description_appended"])


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    ADMINS=[],
)
class DataCiteHarvestEntryPointTest(TestCase):
    @responses.activate
    def test_harvest_datacite_sends_success_email(self):
        source = Source.objects.create(
            name="SESAR entrypoint",
            url_field="https://api.datacite.org/dois",
            source_type="datacite",
            harvest_config={"publisher": "SESAR", "require_coordinates": True, "sort": "created"},
        )
        responses.add(
            responses.GET,
            DATACITE_API_URL,
            json=_response([_record("10.58052/ffff6666", lat=10, lon=20)]),
            status=200,
        )
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username="harvester", email="harvester@example.org")
        harvest_datacite(source.id, user=user, max_records=10)

        self.assertEqual(Work.objects.filter(type="physical-object").count(), 1)
        self.assertTrue(mail.outbox)
        body = mail.outbox[0].body
        self.assertIn("New samples saved", body)


@tag("online")
class DataCiteOnlineSmokeTest(TestCase):
    """Hits the real DataCite API with a tiny cap; self-skips on network error."""

    def test_live_sesar_harvest(self):
        import requests

        source = Source.objects.create(
            name="SESAR online smoke",
            url_field="https://api.datacite.org/dois",
            source_type="datacite",
            harvest_config={"publisher": "SESAR", "require_coordinates": True, "sort": "created"},
        )
        try:
            harvest_datacite(source.id, max_records=3)
        except (requests.exceptions.RequestException, RuntimeError) as e:
            self.skipTest(f"DataCite unreachable: {e}")

        works = Work.objects.filter(type="physical-object")
        self.assertGreaterEqual(works.count(), 1)
        for w in works:
            self.assertIsNotNone(w.geometry)
            self.assertEqual(w.status, "p")
