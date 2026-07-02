# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the GFZ IGSN landing-page scraping harvester (#187)."""

from urllib.parse import parse_qs, urlparse

import responses
from django.core import mail
from django.test import TestCase, override_settings

from works.harvesting.gfz_igsn import _parse_gfz_location, harvest_gfz_igsn, scrape_gfz_and_save_works
from works.harvesting.sessions import DATACITE_API_URL, GFZ_LANDING_URL
from works.models import Source, Work, WorkRelation

# Minimal GFZ landing-page shapes (mirrors the real <table class="location">).
_PAGE_WITH_COORDS = """
<html><body><h1>Sampling Location</h1>
<table class="location">
  <tr><td>Latitude:</td><td>30.342897</td></tr>
  <tr><td>Longitude:</td><td>77.691147</td></tr>
  <tr><td>Coordinate System:</td><td>WGS84</td></tr>
  <tr><td>Elevation:</td><td>532.0</td></tr>
</table></body></html>
"""

_PAGE_NA = """
<html><body><table class="location">
  <tr><td>Latitude:</td><td>N/A</td></tr>
  <tr><td>Longitude:</td><td>N/A</td></tr>
</table></body></html>
"""


def _dc_record(doi, related=None):
    return {
        "id": doi,
        "type": "dois",
        "attributes": {
            "doi": doi,
            "publisher": "GFZ Data Services",
            "titles": [{"title": f"Sample {doi}"}],
            "creators": [{"name": "Doe, Jane"}],
            "url": f"https://dataservices.gfz-potsdam.de/igsn/esg/index.php?igsn={doi.rsplit('/', 1)[-1].upper()}",
            "geoLocations": [],
            "relatedIdentifiers": related or [],
            "dates": [],
        },
    }


class GfzLocationParseTest(TestCase):
    def test_parses_point(self):
        point = _parse_gfz_location(_PAGE_WITH_COORDS)
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point.x, 77.691147)
        self.assertAlmostEqual(point.y, 30.342897)

    def test_na_returns_none(self):
        self.assertIsNone(_parse_gfz_location(_PAGE_NA))

    def test_missing_table_returns_none(self):
        self.assertIsNone(_parse_gfz_location("<html><body>no table</body></html>"))


class GfzScrapeTest(TestCase):
    def setUp(self):
        from works.harvesting.common import ensure_collection_for_source

        self.source = Source.objects.create(
            name="GFZ test",
            url_field="https://dataservices.gfz-potsdam.de/igsn/",
            source_type="gfz-igsn",
        )
        ensure_collection_for_source(self.source)

    def _landing_callback(self, request):
        igsn = (parse_qs(urlparse(request.url).query).get("igsn") or [""])[0]
        html = _PAGE_NA if "NACOORD" in igsn else _PAGE_WITH_COORDS
        return (200, {}, html)

    def _register(self, records):
        responses.add(responses.GET, DATACITE_API_URL, json={"data": records, "links": {}}, status=200)
        responses.add_callback(responses.GET, GFZ_LANDING_URL, callback=self._landing_callback)

    @responses.activate
    def test_scrapes_coords_and_skips_na(self):
        self._register(
            [
                _dc_record("10.60510/gfhwo0029"),
                _dc_record("10.60510/nacoord01"),  # landing page shows N/A -> skipped
            ]
        )
        from works.harvesting.common import HarvestStats
        from works.models import HarvestingEvent

        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")
        stats = HarvestStats()
        saved, seen = scrape_gfz_and_save_works(self.source, event, stats=stats, throttle=0)

        self.assertEqual(seen, 2)
        self.assertEqual(saved, 1)
        w = Work.objects.get(doi="10.60510/gfhwo0029")
        self.assertEqual(w.type, "physical-object")
        self.assertEqual(w.status, "p")
        self.assertIsNotNone(w.geometry)
        self.assertEqual(w.provenance["metadata_sources"]["geometry"], "gfz.landing_page")
        self.assertTrue(w.collections.exists())

    @responses.activate
    def test_relations_from_datacite(self):
        self._register(
            [
                _dc_record(
                    "10.60510/gfrel0001",
                    related=[
                        {
                            "relationType": "IsPartOf",
                            "relatedIdentifier": "10.5880/gfz.parent",
                            "relatedIdentifierType": "DOI",
                        }
                    ],
                ),
            ]
        )
        from works.harvesting.common import HarvestStats
        from works.models import HarvestingEvent

        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")
        scrape_gfz_and_save_works(self.source, event, stats=HarvestStats(), throttle=0)
        sample = Work.objects.get(doi="10.60510/gfrel0001")
        rel = WorkRelation.objects.get(from_work=sample)
        self.assertEqual(rel.relation_type, "is_part_of")
        self.assertEqual(rel.to_identifier, "10.5880/gfz.parent")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", ADMINS=[])
class GfzEntryPointTest(TestCase):
    @responses.activate
    def test_harvest_gfz_igsn_sends_email(self):
        source = Source.objects.create(
            name="GFZ entrypoint",
            url_field="https://dataservices.gfz-potsdam.de/igsn/",
            source_type="gfz-igsn",
        )
        responses.add(
            responses.GET,
            DATACITE_API_URL,
            json={"data": [_dc_record("10.60510/gfmail001")], "links": {}},
            status=200,
        )
        responses.add(responses.GET, GFZ_LANDING_URL, body=_PAGE_WITH_COORDS, status=200)

        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username="gfzharv", email="gfz@example.org")
        harvest_gfz_igsn(source.id, user=user, throttle=0)

        self.assertEqual(Work.objects.filter(type="physical-object").count(), 1)
        self.assertTrue(mail.outbox)
        self.assertIn("New samples saved", mail.outbox[0].body)
