# SPDX-FileCopyrightText: 2023 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import django
import responses
from django.test import Client, TestCase, tag
from django.utils import timezone

# bootstrap Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model

from works.models import HarvestingEvent, Schedule, Source, Work
from works.tasks import (
    extract_geometry_from_html,
    extract_timeperiod_from_html,
    harvest_oai_endpoint,
    harvest_rss_endpoint,
    parse_oai_xml_and_save_works,
    parse_rss_feed_and_save_publications,
)

User = get_user_model()
BASE_TEST_DIR = Path(__file__).resolve().parent


class SimpleTest(TestCase):
    @responses.activate
    def setUp(self):
        super().setUp()

        Work.objects.all().delete()

        article01_path = BASE_TEST_DIR / "harvesting" / "source_1" / "article_01.html"
        article02_path = BASE_TEST_DIR / "harvesting" / "source_1" / "article_02.html"
        with open(article01_path) as f1, open(article02_path) as f2:
            responses.add(responses.GET, "http://localhost:8330/index.php/opti-geo/article/view/1", body=f1.read())
            responses.add(responses.GET, "http://localhost:8330/index.php/opti-geo/article/view/2", body=f2.read())

        src = Source.objects.create(url_field="http://example.org/oai", harvest_interval_minutes=60)
        event = HarvestingEvent.objects.create(source=src, status="in_progress")

        oai_path = BASE_TEST_DIR / "harvesting" / "source_1" / "oai_dc.xml"
        xml_bytes = oai_path.read_bytes()
        parse_oai_xml_and_save_works(xml_bytes, event)

        Work.objects.all().update(status="p")

        self.user = User.objects.create_user(username="testuser", email="testuser@example.com", password="password123")
        self.client = Client()

        results = self.client.get("/api/v1/works/").json()["results"]
        features = results.get("features", [])
        if len(features) >= 2:
            self.id1, self.id2 = features[1]["id"], features[0]["id"]
        elif len(features) == 1:
            self.id1 = self.id2 = features[0]["id"]
        else:
            self.id1 = self.id2 = None

    def test_api_root(self):
        response = self.client.get("/api/v1/works/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get("Content-Type"), "application/geo+json")
        results = response.json()["results"]
        self.assertEqual(results["type"], "FeatureCollection")
        self.assertEqual(len(results["features"]), 2)

    def test_api_publication_1(self):
        response = self.client.get(f"/api/v1/works/{self.id1}.json")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], "Feature")
        self.assertEqual(body["geometry"]["type"], "GeometryCollection")
        self.assertEqual(body["geometry"]["geometries"][0]["type"], "LineString")
        self.assertEqual(body["properties"]["title"], "Test 1: One")
        self.assertEqual(body["properties"]["publicationDate"], "2022-07-01")
        self.assertEqual(body["properties"]["timeperiod_startdate"], ["2022-06-01"])
        self.assertEqual(body["properties"]["url"], "http://localhost:8330/index.php/opti-geo/article/view/1")

    def test_api_publication_2(self):
        response = self.client.get(f"/api/v1/works/{self.id2}.json")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], "Feature")
        self.assertEqual(body["geometry"]["type"], "GeometryCollection")
        self.assertEqual(body["geometry"]["geometries"][0]["type"], "Polygon")
        self.assertEqual(body["properties"]["title"], "Test 2: Two")
        self.assertIsNone(body["properties"]["doi"])
        self.assertEqual(body["properties"]["timeperiod_enddate"], ["2022-03-31"])
        self.assertEqual(body["properties"]["url"], "http://localhost:8330/index.php/opti-geo/article/view/2")

        props = body["properties"]
        self.assertEqual(props["title"], "Test 2: Two")
        self.assertIsNone(props["doi"])
        self.assertEqual(props["timeperiod_enddate"], ["2022-03-31"])
        self.assertEqual(props["url"], "http://localhost:8330/index.php/opti-geo/article/view/2")

    def test_task_scheduling(self):
        oai_file_path = BASE_TEST_DIR / "harvesting" / "journal_1" / "oai_dc.xml"
        new_src = Source.objects.create(url_field=f"file://{oai_file_path}", harvest_interval_minutes=60)
        time.sleep(2)
        schedule_q = Schedule.objects.filter(name=f"Harvest Source {new_src.id}")
        self.assertTrue(schedule_q.exists(), "Django-Q task not scheduled for source.")

    def test_no_duplicates(self):
        publications = Work.objects.all()
        self.assertEqual(publications.count(), 2, "Expected exactly 2 unique publications")
        titles = [p.title for p in publications]
        self.assertEqual(len(titles), len(set(titles)), "Duplicate titles found")

    def test_invalid_xml_input(self):
        src = Source.objects.create(url_field="http://example.org/invalid", harvest_interval_minutes=60)
        event = HarvestingEvent.objects.create(source=src, status="in_progress")

        invalid_xml = b"<invalid>malformed xml without proper closing"
        initial_count = Work.objects.count()

        parse_oai_xml_and_save_works(invalid_xml, event)

        self.assertEqual(Work.objects.count(), initial_count)

    def test_empty_xml_input(self):
        """Test harvesting with empty XML input"""
        src = Source.objects.create(url_field="http://example.org/empty", harvest_interval_minutes=60)
        event = HarvestingEvent.objects.create(source=src, status="in_progress")

        empty_xml = b""
        initial_count = Work.objects.count()

        parse_oai_xml_and_save_works(empty_xml, event)

        self.assertEqual(Work.objects.count(), initial_count)

    def test_xml_with_no_records(self):
        """Test harvesting with valid XML but no record elements"""
        src = Source.objects.create(url_field="http://example.org/norecords", harvest_interval_minutes=60)
        event = HarvestingEvent.objects.create(source=src, status="in_progress")

        no_records_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
            <responseDate>2024-01-01T00:00:00Z</responseDate>
            <request verb="ListRecords">http://example.org/oai</request>
            <ListRecords>
                <!-- No record elements -->
            </ListRecords>
        </OAI-PMH>"""

        initial_count = Work.objects.count()

        parse_oai_xml_and_save_works(no_records_xml, event)

        self.assertEqual(Work.objects.count(), initial_count)

    def test_xml_with_invalid_record_data(self):
        src = Source.objects.create(url_field="http://example.org/invaliddata", harvest_interval_minutes=60)
        event = HarvestingEvent.objects.create(source=src, status="in_progress")

        # XML with record but missing required fields
        invalid_data_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
            <responseDate>2024-01-01T00:00:00Z</responseDate>
            <request verb="ListRecords">http://example.org/oai</request>
            <ListRecords>
                <record>
                    <header>
                        <identifier>oai:example.org:123</identifier>
                        <datestamp>2024-01-01</datestamp>
                    </header>
                    <metadata>
                        <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
                                   xmlns:dc="http://purl.org/dc/elements/1.1/">
                            <!-- Missing title and identifier -->
                            <dc:description>Some description</dc:description>
                        </oai_dc:dc>
                    </metadata>
                </record>
            </ListRecords>
        </OAI-PMH>"""

        initial_count = Work.objects.count()

        parse_oai_xml_and_save_works(invalid_data_xml, event)

        self.assertEqual(Work.objects.count(), initial_count)

    @staticmethod
    def _skip_if_oai_unreachable(url, expect_ok=True):
        import requests as _requests

        try:
            r = _requests.head(url, timeout=8, allow_redirects=True)
        except _requests.RequestException as e:
            raise unittest.SkipTest(f"endpoint unreachable: {url}: {e}")
        # Some OAI servers reject HEAD with 405 — fall back to a tiny GET.
        if r.status_code in (405, 501):
            try:
                r = _requests.get(url, timeout=8, stream=True)
            except _requests.RequestException as e:
                raise unittest.SkipTest(f"endpoint unreachable: {url}: {e}")
        if expect_ok and not r.ok:
            raise unittest.SkipTest(
                f"endpoint returned HTTP {r.status_code} for {url} — upstream is down/moved, skipping live test"
            )

    @tag("online")
    def test_real_journal_harvesting_essd(self):
        """Test harvesting from actual ESSD Copernicus endpoint"""
        from works.tasks import harvest_oai_endpoint

        url = "https://oai-pmh.copernicus.org/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=essd"
        self._skip_if_oai_unreachable(url)

        # Clear existing publications for clean test
        Work.objects.all().delete()

        src = Source.objects.create(url_field=url, harvest_interval_minutes=1440, name="ESSD Copernicus")

        initial_count = Work.objects.count()

        # Harvest from real endpoint with limit
        harvest_oai_endpoint(src.id, max_records=3)

        # Should have harvested some publications
        final_count = Work.objects.count()
        self.assertGreater(final_count, initial_count, "Should harvest at least some publications from ESSD")
        self.assertLessEqual(final_count - initial_count, 3, "Should not exceed max_records limit")

        # Verify ESSD publications were created
        essd_pubs = Work.objects.filter(source=src)
        for pub in essd_pubs:
            self.assertIsNotNone(pub.title, f"Publication {pub.id} missing title")
            self.assertIsNotNone(pub.url, f"Publication {pub.id} missing URL")
            # ESSD should have DOIs with Copernicus prefix
            if pub.doi:
                self.assertIn("10.5194", pub.doi, "ESSD DOIs should contain Copernicus prefix")

    @tag("online")
    def test_real_journal_harvesting_geo_leo(self):
        """Test harvesting from actual GEO-LEO e-docs endpoint"""
        from works.tasks import harvest_oai_endpoint

        url = "https://e-docs.geo-leo.de/server/oai/request"
        self._skip_if_oai_unreachable(url)

        # Clear existing publications for clean test
        Work.objects.all().delete()

        src = Source.objects.create(url_field=url, harvest_interval_minutes=1440, name="GEO-LEO e-docs")

        initial_count = Work.objects.count()

        # Harvest from real endpoint with limit
        harvest_oai_endpoint(src.id, max_records=5)

        # Should have harvested some publications
        final_count = Work.objects.count()
        self.assertGreater(final_count, initial_count, "Should harvest at least some publications from GEO-LEO")
        self.assertLessEqual(final_count - initial_count, 5, "Should not exceed max_records limit")

        # Verify GEO-LEO publications were created
        geo_leo_pubs = Work.objects.filter(source=src)
        for pub in geo_leo_pubs:
            self.assertIsNotNone(pub.title, f"Publication {pub.id} missing title")
            self.assertIsNotNone(pub.url, f"Publication {pub.id} missing URL")

    @tag("online")
    def test_real_journal_harvesting_agile_giss(self):
        """Test harvesting from actual AGILE-GISS endpoint"""
        from works.tasks import harvest_oai_endpoint

        # Clear existing publications for clean test
        Work.objects.all().delete()

        src = Source.objects.create(
            url_field="https://www.agile-giscience-series.net", harvest_interval_minutes=1440, name="AGILE-GISS"
        )

        initial_count = Work.objects.count()

        # Note: This may fail if AGILE doesn't have OAI-PMH endpoint
        try:
            harvest_oai_endpoint(src.id, max_records=3)

            # Should have harvested some publications
            final_count = Work.objects.count()
            self.assertGreater(final_count, initial_count, "Should harvest at least some publications from AGILE-GISS")
            self.assertLessEqual(final_count - initial_count, 3, "Should not exceed max_records limit")

            # Verify AGILE publications were created
            agile_pubs = Work.objects.filter(source=src)
            for pub in agile_pubs:
                self.assertIsNotNone(pub.title, f"Publication {pub.id} missing title")
                self.assertIsNotNone(pub.url, f"Publication {pub.id} missing URL")
        except Exception as e:
            # Skip test if AGILE doesn't have OAI-PMH endpoint
            self.skipTest(f"AGILE-GISS endpoint not available: {e}")


class OpenAlexLocationsOnlineTests(TestCase):
    """Live check that a real OpenAlex work payload yields normalised locations."""

    @tag("online")
    def test_real_openalex_payload_has_locations(self):
        import requests as _requests

        from works.harvesting.openalex_locations import build_locations

        try:
            # A well-known multi-version work (ESSD article also on a preprint server).
            r = _requests.get("https://api.openalex.org/works/doi:10.5194/essd-13-4349-2021", timeout=15)
            r.raise_for_status()
        except _requests.RequestException as e:
            self.skipTest(f"OpenAlex unreachable: {e}")

        locations = build_locations(r.json())
        self.assertGreaterEqual(len(locations), 1, "Real OpenAlex work should yield at least one location")
        self.assertTrue(all(loc["credit"] == "openalex" for loc in locations))
        self.assertTrue(any(loc.get("is_primary") for loc in locations))


class HarvestingErrorTests(TestCase):
    """
    Test cases for error handling during harvesting.

    These tests verify that the harvesting system properly handles:
    - Malformed XML
    - Empty responses
    - Missing required metadata
    - Invalid XML structure
    - Network/HTTP errors
    """

    def setUp(self):
        """Set up test sources and events."""
        Work.objects.all().delete()
        self.source = Source.objects.create(
            url_field="http://example.com/oai", harvest_interval_minutes=60, name="Error Test Source"
        )

    def test_malformed_xml(self):
        """Test that malformed XML is handled gracefully."""
        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

        malformed_xml_path = BASE_TEST_DIR / "harvesting" / "error_cases" / "malformed_xml.xml"
        xml_bytes = malformed_xml_path.read_bytes()

        # Should not raise exception, but should log error
        parse_oai_xml_and_save_works(xml_bytes, event)

        # No publications should be created from malformed XML
        pub_count = Work.objects.filter(job=event).count()
        self.assertEqual(pub_count, 0, "Malformed XML should not create publications")

    def test_empty_response(self):
        """Test that empty OAI-PMH response (no records) is handled."""
        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

        empty_xml_path = BASE_TEST_DIR / "harvesting" / "error_cases" / "empty_response.xml"
        xml_bytes = empty_xml_path.read_bytes()

        # Should not raise exception
        parse_oai_xml_and_save_works(xml_bytes, event)

        # No publications should be created from empty response
        pub_count = Work.objects.filter(job=event).count()
        self.assertEqual(pub_count, 0, "Empty response should create zero publications")

    def test_invalid_xml_structure(self):
        """Test that non-OAI-PMH XML structure is handled."""
        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

        invalid_xml_path = BASE_TEST_DIR / "harvesting" / "error_cases" / "invalid_xml_structure.xml"
        xml_bytes = invalid_xml_path.read_bytes()

        # Should not raise exception
        parse_oai_xml_and_save_works(xml_bytes, event)

        # No publications should be created from invalid structure
        pub_count = Work.objects.filter(job=event).count()
        self.assertEqual(pub_count, 0, "Invalid XML structure should create zero publications")

    def test_missing_required_metadata(self):
        """Test that records with missing required fields are handled."""
        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

        missing_metadata_path = BASE_TEST_DIR / "harvesting" / "error_cases" / "missing_metadata.xml"
        xml_bytes = missing_metadata_path.read_bytes()

        # Should not raise exception - may create some publications
        parse_oai_xml_and_save_works(xml_bytes, event)

        # Check what was created
        pubs = Work.objects.filter(job=event)

        # At least one record (the one with title) should be created
        self.assertGreaterEqual(pubs.count(), 1, "Should create publications even with minimal metadata")

        # Check that publications were created despite missing fields
        for pub in pubs:
            # Title might be None for some records
            if pub.title:
                self.assertIsInstance(pub.title, str)

    def test_empty_content(self):
        """Test that empty/None content is handled."""
        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

        # Test with empty bytes
        parse_oai_xml_and_save_works(b"", event)
        pub_count = Work.objects.filter(job=event).count()
        self.assertEqual(pub_count, 0, "Empty content should create zero publications")

        # Test with whitespace only
        parse_oai_xml_and_save_works(b"   \n\t  ", event)
        pub_count = Work.objects.filter(job=event).count()
        self.assertEqual(pub_count, 0, "Whitespace-only content should create zero publications")

    @responses.activate
    def test_http_404_error(self):
        """Test that HTTP 404 errors are handled properly."""
        # Mock a 404 response
        responses.add(responses.GET, "http://example.com/oai-404", status=404, body="Not Found")

        source = Source.objects.create(url_field="http://example.com/oai-404", harvest_interval_minutes=60)

        # harvest_oai_endpoint should handle the error
        harvest_oai_endpoint(source.id)

        # Check that event was marked as failed
        event = HarvestingEvent.objects.filter(source=source).latest("started_at")
        self.assertEqual(event.status, "failed", "Event should be marked as failed for 404 error")

    @responses.activate
    def test_http_500_error(self):
        """Test that HTTP 500 errors are handled properly."""
        # Mock a 500 response
        responses.add(responses.GET, "http://example.com/oai-500", status=500, body="Internal Server Error")

        source = Source.objects.create(url_field="http://example.com/oai-500", harvest_interval_minutes=60)

        # harvest_oai_endpoint should handle the error
        harvest_oai_endpoint(source.id)

        # Check that event was marked as failed
        event = HarvestingEvent.objects.filter(source=source).latest("started_at")
        self.assertEqual(event.status, "failed", "Event should be marked as failed for 500 error")

    @responses.activate
    def test_network_timeout(self):
        """Test that network timeouts are handled properly."""
        from requests.exceptions import Timeout

        # Mock a timeout
        responses.add(responses.GET, "http://example.com/oai-timeout", body=Timeout("Connection timeout"))

        source = Source.objects.create(url_field="http://example.com/oai-timeout", harvest_interval_minutes=60)

        # harvest_oai_endpoint should handle the timeout
        harvest_oai_endpoint(source.id)

        # Check that event was marked as failed
        event = HarvestingEvent.objects.filter(source=source).latest("started_at")
        self.assertEqual(event.status, "failed", "Event should be marked as failed for timeout")

    @responses.activate
    def test_invalid_xml_in_http_response(self):
        """A 200 response whose body is not XML is now rejected by the
        content sniffer in ``harvest_oai_endpoint`` rather than fed to the XML
        parser and silently producing zero works. (Pre-hardening this test
        asserted the harvest "completed"; that masked the real failure mode
        we hit with Copernicus, which returns an HTML 200 maintenance page.)
        """
        responses.add(
            responses.GET,
            "http://example.com/oai-invalid",
            status=200,
            body="This is not XML at all",
            content_type="text/xml",
        )

        source = Source.objects.create(url_field="http://example.com/oai-invalid", harvest_interval_minutes=60)

        harvest_oai_endpoint(source.id)

        event = HarvestingEvent.objects.filter(source=source).latest("started_at")
        self.assertEqual(event.status, "failed", "Non-XML body should now mark the event failed")
        self.assertEqual(Work.objects.filter(job=event).count(), 0, "Invalid XML should create zero publications")

    def test_max_records_limit_with_errors(self):
        """Test that max_records works even when some records cause errors."""
        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

        # Use the missing metadata file which has 2 records, one problematic
        missing_metadata_path = BASE_TEST_DIR / "harvesting" / "error_cases" / "missing_metadata.xml"
        xml_bytes = missing_metadata_path.read_bytes()

        # Limit to 1 record
        parse_oai_xml_and_save_works(xml_bytes, event, max_records=1)

        # Should process only 1 record
        pub_count = Work.objects.filter(job=event).count()
        self.assertLessEqual(pub_count, 1, "Should respect max_records limit even with errors")


class OaiTemporalExtractionGuardTests(TestCase):
    """Regression: extract_timeperiod_from_html() returns [None]/[None] both
    for a genuine open-ended interval (one side real) and for "the landing
    page has no temporal metadata at all" — parse_oai_xml_and_save_works must
    only persist that sentinel when it actually carries a real value on
    either side, or every work with no temporal metadata ends up looking
    exactly like one with a genuine (if open-ended) extent."""

    def setUp(self):
        Work.objects.all().delete()
        self.source = Source.objects.create(
            url_field="http://example.com/oai", harvest_interval_minutes=60, name="Temporal Guard Test Source"
        )

    @responses.activate
    def test_no_temporal_metadata_on_page_leaves_arrays_empty(self):
        responses.add(
            responses.GET,
            "http://example.com/article/1",
            body="<html><head><title>No dates here</title></head></html>",
        )
        xml_bytes = b"""<?xml version="1.0" encoding="UTF-8"?>
        <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
          <ListRecords>
            <record>
              <header><identifier>oai:example:1</identifier></header>
              <metadata>
                <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
                    xmlns:dc="http://purl.org/dc/elements/1.1/">
                  <dc:title>No Temporal Data</dc:title>
                  <dc:identifier>http://example.com/article/1</dc:identifier>
                </oai_dc:dc>
              </metadata>
            </record>
          </ListRecords>
        </OAI-PMH>"""

        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")
        parse_oai_xml_and_save_works(xml_bytes, event)

        work = Work.objects.get(job=event)
        self.assertEqual(work.timeperiod_startdate, [])
        self.assertEqual(work.timeperiod_enddate, [])

    def test_landing_page_fetch_exceeding_deadline_still_saves_the_work(self):
        # Regression: a landing-page fetch that hangs past its wall-clock
        # deadline (fetch_with_deadline raises TimeoutError) must degrade
        # like any other fetch failure — the record is still saved (without
        # geometry/temporal data), not lost or left to crash the harvest.
        xml_bytes = b"""<?xml version="1.0" encoding="UTF-8"?>
        <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
          <ListRecords>
            <record>
              <header><identifier>oai:example:2</identifier></header>
              <metadata>
                <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
                    xmlns:dc="http://purl.org/dc/elements/1.1/">
                  <dc:title>Slow Landing Page</dc:title>
                  <dc:identifier>http://example.com/article/2</dc:identifier>
                </oai_dc:dc>
              </metadata>
            </record>
          </ListRecords>
        </OAI-PMH>"""

        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")
        with patch("works.harvesting.oai.fetch_with_deadline", side_effect=TimeoutError("exceeded deadline")):
            parse_oai_xml_and_save_works(xml_bytes, event)

        work = Work.objects.get(job=event)
        self.assertEqual(work.title, "Slow Landing Page")
        self.assertTrue(work.geometry.empty)


class HarvestingHttpHardeningTests(TestCase):
    """Coverage for the OAI-PMH fetch hardening — content-type sniffing,
    retries on transient errors, and informative error messages so operators
    can diagnose failures from logs/email summaries (issue: revisit failing
    Copernicus imports).
    """

    @responses.activate
    def test_html_404_error_page_is_detected_as_non_xml(self):
        # This is the actual Copernicus failure mode: HTTP 404 with an HTML
        # error body. Pre-hardening, only the status code surfaced; the
        # operator had no quick clue what the upstream actually returned.
        responses.add(
            responses.GET,
            "http://example.com/oai-html-404",
            status=404,
            content_type="text/html",
            body="<!DOCTYPE HTML><html><body><h1>Not Found</h1></body></html>",
        )
        src = Source.objects.create(
            url_field="http://example.com/oai-html-404",
            harvest_interval_minutes=60,
        )
        harvest_oai_endpoint(src.id)
        event = HarvestingEvent.objects.filter(source=src).latest("started_at")
        self.assertEqual(event.status, "failed")

    @responses.activate
    def test_html_200_error_page_is_rejected_before_xml_parse(self):
        # Some upstreams return a 200 with an HTML page when the OAI handler
        # is misconfigured. The hardening must reject the body up front
        # rather than handing HTML to the XML parser.
        responses.add(
            responses.GET,
            "http://example.com/oai-html-200",
            status=200,
            content_type="text/html",
            body="<!DOCTYPE HTML><html><body>Maintenance window</body></html>",
        )
        src = Source.objects.create(
            url_field="http://example.com/oai-html-200",
            harvest_interval_minutes=60,
        )
        harvest_oai_endpoint(src.id)
        event = HarvestingEvent.objects.filter(source=src).latest("started_at")
        self.assertEqual(event.status, "failed", "Non-XML 200 response must mark the event failed")
        # And no Works should have been created from the HTML body.
        self.assertEqual(Work.objects.filter(job=event).count(), 0)

    @responses.activate
    def test_retry_on_transient_503_then_success(self):
        # urllib3's retry adapter consumes the registered responses one by
        # one. Two 503s, then a small valid OAI response — the harvest must
        # complete cleanly without surfacing the transient errors.
        for _ in range(2):
            responses.add(
                responses.GET,
                "http://example.com/oai-flaky",
                status=503,
                body="Service Unavailable",
            )
        oai_path = BASE_TEST_DIR / "harvesting" / "source_1" / "oai_dc.xml"
        responses.add(
            responses.GET,
            "http://example.com/oai-flaky",
            status=200,
            content_type="text/xml",
            body=oai_path.read_bytes(),
        )
        src = Source.objects.create(
            url_field="http://example.com/oai-flaky",
            harvest_interval_minutes=60,
        )
        harvest_oai_endpoint(src.id)
        event = HarvestingEvent.objects.filter(source=src).latest("started_at")
        self.assertEqual(event.status, "completed", "Transient 503s must be retried until success")

    @responses.activate
    def test_user_agent_header_is_set(self):
        # Identifies our harvester traffic to upstream operators (so e.g.
        # Copernicus can contact us if we hammer them).
        oai_path = BASE_TEST_DIR / "harvesting" / "source_1" / "oai_dc.xml"
        responses.add(
            responses.GET,
            "http://example.com/oai-ua",
            status=200,
            content_type="text/xml",
            body=oai_path.read_bytes(),
        )
        src = Source.objects.create(
            url_field="http://example.com/oai-ua",
            harvest_interval_minutes=60,
        )
        harvest_oai_endpoint(src.id)
        sent_ua = responses.calls[0].request.headers.get("User-Agent", "")
        self.assertIn("OPTIMAP", sent_ua)


class ChunkedHarvestingTests(TestCase):
    """OAI-PMH year-chunk harvesting: every harvest iterates all calendar
    years (latest first) via ``from``/``until`` so no single request asks
    the server for its full history.  Existing records are skipped by the
    normal dedup logic in ``parse_oai_xml_and_save_works``."""

    _NO_RECORDS_XML = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        b'<error code="noRecordsMatch">No matching records</error>'
        b"</OAI-PMH>"
    )

    def _identify_xml(self):
        """Minimal Identify with earliestDatestamp set to last year (2 chunks total)."""
        prev = str(timezone.now().year - 1)
        return (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            b"<Identify><earliestDatestamp>" + prev.encode() + b"-01-01T00:00:00Z</earliestDatestamp></Identify>"
            b"</OAI-PMH>"
        )

    @responses.activate
    def test_year_chunks_requested_latest_first(self):
        oai_path = BASE_TEST_DIR / "harvesting" / "source_1" / "oai_dc.xml"

        # Identify → current-year chunk (valid records) → previous-year chunk (empty)
        responses.add(
            responses.GET, "http://example.com/oai-chunk-order", body=self._identify_xml(), content_type="text/xml"
        )
        responses.add(
            responses.GET,
            "http://example.com/oai-chunk-order",
            status=200,
            content_type="text/xml",
            body=oai_path.read_bytes(),
        )
        responses.add(
            responses.GET,
            "http://example.com/oai-chunk-order",
            status=200,
            content_type="text/xml",
            body=self._NO_RECORDS_XML,
        )

        src = Source.objects.create(url_field="http://example.com/oai-chunk-order", harvest_interval_minutes=60)
        harvest_oai_endpoint(src.id)

        oai_calls = [c for c in responses.calls if "example.com/oai-chunk-order" in c.request.url]

        # First call: Identify
        self.assertIn("verb=Identify", oai_calls[0].request.url)

        # Second call: current-year chunk
        current_year = str(timezone.now().year)
        self.assertIn(f"from={current_year}-01-01", oai_calls[1].request.url)
        self.assertIn(f"until={current_year}-12-31", oai_calls[1].request.url)

        # Third call: previous-year chunk
        prev_year = str(timezone.now().year - 1)
        self.assertIn(f"from={prev_year}-01-01", oai_calls[2].request.url)
        self.assertIn(f"until={prev_year}-12-31", oai_calls[2].request.url)

        event = HarvestingEvent.objects.filter(source=src).latest("started_at")
        self.assertEqual(event.status, "completed")

    @responses.activate
    def test_no_records_match_skips_chunk(self):
        oai_path = BASE_TEST_DIR / "harvesting" / "source_1" / "oai_dc.xml"

        # Identify → current-year has no records → previous-year has records
        responses.add(
            responses.GET, "http://example.com/oai-nomatch", body=self._identify_xml(), content_type="text/xml"
        )
        responses.add(
            responses.GET,
            "http://example.com/oai-nomatch",
            status=200,
            content_type="text/xml",
            body=self._NO_RECORDS_XML,
        )
        responses.add(
            responses.GET,
            "http://example.com/oai-nomatch",
            status=200,
            content_type="text/xml",
            body=oai_path.read_bytes(),
        )

        src = Source.objects.create(url_field="http://example.com/oai-nomatch", harvest_interval_minutes=60)
        harvest_oai_endpoint(src.id, max_records=2)

        event = HarvestingEvent.objects.filter(source=src).latest("started_at")
        self.assertEqual(event.status, "completed")
        self.assertGreater(Work.objects.filter(job=event).count(), 0, "Records from the non-empty chunk must be saved")

    @responses.activate
    def test_explicit_date_in_url_skips_chunking(self):
        # When the source URL already carries ``from=``, the harvester must
        # use it as-is — no Identify call, no year splitting.
        oai_path = BASE_TEST_DIR / "harvesting" / "source_1" / "oai_dc.xml"
        responses.add(
            responses.GET,
            "http://example.com/oai-explicit",
            status=200,
            content_type="text/xml",
            body=oai_path.read_bytes(),
        )

        src = Source.objects.create(
            url_field="http://example.com/oai-explicit?verb=ListRecords&metadataPrefix=oai_dc&from=2020-01-01",
            harvest_interval_minutes=60,
        )
        harvest_oai_endpoint(src.id)

        # Filter to ListRecords harvest calls only; a separate ListIdentifiers stats
        # probe runs after harvest completion and must not be counted here.
        harvest_calls = [
            c
            for c in responses.calls
            if "example.com/oai-explicit" in c.request.url and "ListRecords" in c.request.url
        ]
        self.assertEqual(len(harvest_calls), 1, "Exactly one harvest request when URL has an explicit date filter")
        self.assertNotIn("verb=Identify", harvest_calls[0].request.url)
        self.assertIn("from=2020-01-01", harvest_calls[0].request.url)
        self.assertEqual(harvest_calls[0].request.url.count("from="), 1)


class RSSFeedHarvestingTests(TestCase):
    """
    Test cases for RSS/Atom feed harvesting.

    These tests verify that the RSS harvesting system properly handles:
    - RDF/RSS feed parsing
    - Publication extraction from feed entries
    - Duplicate detection
    - DOI and metadata extraction
    """

    def setUp(self):
        """Set up test source for RSS feeds."""
        Work.objects.all().delete()
        self.source = Source.objects.create(
            url_field="https://www.example.com/feed.rss", harvest_interval_minutes=60, name="Test RSS Source"
        )

    def test_parse_rss_feed_from_file(self):
        """Test parsing RSS feed from local file."""
        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

        rss_feed_path = BASE_TEST_DIR / "harvesting" / "rss_feed_sample.xml"
        feed_url = f"file://{rss_feed_path}"

        processed, saved = parse_rss_feed_and_save_publications(feed_url, event)

        # Check counts
        self.assertEqual(processed, 2, "Should process 2 entries")
        self.assertEqual(saved, 2, "Should save 2 publications")

        # Check created publications
        pubs = Work.objects.filter(job=event)
        self.assertEqual(pubs.count(), 2)

        # Check first publication
        pub1 = pubs.filter(doi="10.1234/test-001").first()
        self.assertIsNotNone(pub1)
        self.assertEqual(pub1.title, "Test Article One: Data Repository")
        self.assertEqual(pub1.url, "https://www.example.com/articles/test-article-1")
        self.assertEqual(str(pub1.publicationDate), "2025-10-01")

        # Check second publication
        pub2 = pubs.filter(doi="10.1234/test-002").first()
        self.assertIsNotNone(pub2)
        self.assertEqual(pub2.title, "Test Article Two: Analysis Methods")

    def test_rss_duplicate_detection_by_doi(self):
        """Test that duplicate detection works by DOI."""
        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

        # Create existing publication with same DOI
        Work.objects.create(
            title="Existing Publication",
            doi="10.1234/test-001",
            source=self.source,
            timeperiod_startdate=[],
            timeperiod_enddate=[],
        )

        rss_feed_path = BASE_TEST_DIR / "harvesting" / "rss_feed_sample.xml"
        feed_url = f"file://{rss_feed_path}"

        processed, saved = parse_rss_feed_and_save_publications(feed_url, event)

        # Should process both but only save one (the one without duplicate DOI)
        self.assertEqual(processed, 2)
        self.assertEqual(saved, 1, "Should only save publication without duplicate DOI")

    def test_rss_duplicate_detection_by_url(self):
        """Test that duplicate detection works by URL."""
        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

        # Create existing publication with same URL
        Work.objects.create(
            title="Existing Publication",
            url="https://www.example.com/articles/test-article-1",
            source=self.source,
            timeperiod_startdate=[],
            timeperiod_enddate=[],
        )

        rss_feed_path = BASE_TEST_DIR / "harvesting" / "rss_feed_sample.xml"
        feed_url = f"file://{rss_feed_path}"

        processed, saved = parse_rss_feed_and_save_publications(feed_url, event)

        # Should process both but only save one
        self.assertEqual(processed, 2)
        self.assertEqual(saved, 1, "Should only save publication without duplicate URL")

    def test_rss_max_records_limit(self):
        """Test that max_records parameter limits RSS harvesting."""
        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

        rss_feed_path = BASE_TEST_DIR / "harvesting" / "rss_feed_sample.xml"
        feed_url = f"file://{rss_feed_path}"

        # Limit to 1 record
        processed, saved = parse_rss_feed_and_save_publications(feed_url, event, max_records=1)

        self.assertEqual(processed, 1, "Should only process 1 entry")
        self.assertEqual(saved, 1, "Should only save 1 publication")

        pubs = Work.objects.filter(job=event)
        self.assertEqual(pubs.count(), 1)

    def test_harvest_rss_endpoint_from_file(self):
        """Test complete RSS harvesting workflow from file."""
        rss_feed_path = BASE_TEST_DIR / "harvesting" / "rss_feed_sample.xml"

        # Update source to use file:// URL
        self.source.url_field = f"file://{rss_feed_path}"
        self.source.save()

        # Harvest
        harvest_rss_endpoint(self.source.id, max_records=10)

        # Check event status
        event = HarvestingEvent.objects.filter(source=self.source).latest("started_at")
        self.assertEqual(event.status, "completed")

        # Check publications
        pubs = Work.objects.filter(job=event)
        self.assertEqual(pubs.count(), 2, "Should create 2 publications from RSS feed")

    def test_harvest_rss_endpoint_invalid_file(self):
        """Test RSS harvesting handles invalid file paths."""
        # Update source to use non-existent file
        self.source.url_field = "file:///tmp/nonexistent_rss_feed.xml"
        self.source.save()

        # Harvest should handle error gracefully
        harvest_rss_endpoint(self.source.id)

        # Check event was marked as completed (feedparser returns empty feed for invalid URLs)
        event = HarvestingEvent.objects.filter(source=self.source).latest("started_at")
        # Event completes but creates no publications
        self.assertEqual(event.status, "completed")

        # No publications should be created
        pubs = Work.objects.filter(job=event)
        self.assertEqual(pubs.count(), 0)

    def test_rss_invalid_feed_url(self):
        """Test handling of invalid RSS feed URL."""
        event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

        # Try to parse non-existent file
        feed_url = "file:///tmp/nonexistent_feed.xml"

        processed, saved = parse_rss_feed_and_save_publications(feed_url, event)

        # Should handle gracefully and return zero
        self.assertEqual(processed, 0)
        self.assertEqual(saved, 0)


JANEWAY_FIXTURES = BASE_TEST_DIR / "harvesting" / "janeway"


def _load_soup(name):
    return BeautifulSoup((JANEWAY_FIXTURES / name).read_text(), "html.parser")


class JanewayGeometadataExtractionTests(TestCase):
    """Covers the JSON-LD / geo+json link / DC.SpatialCoverage / DC.box fallback
    chain emitted by the janeway_geometadata plugin and used by issue #15.
    """

    SULAWESI_BBOX = (119.0, -5.7, 125.0, 1.7)  # west, south, east, north

    def assertEnvelope(self, geom, west, south, east, north, places=4):
        self.assertIsNotNone(geom)
        ext = geom.extent  # (xmin, ymin, xmax, ymax)
        self.assertAlmostEqual(ext[0], west, places=places)
        self.assertAlmostEqual(ext[1], south, places=places)
        self.assertAlmostEqual(ext[2], east, places=places)
        self.assertAlmostEqual(ext[3], north, places=places)

    def test_jsonld_polygon_preferred(self):
        soup = _load_soup("article_jsonld.html")
        geom, label = extract_geometry_from_html(soup)
        self.assertEqual(label, "schema.org JSON-LD")
        self.assertEnvelope(geom, *self.SULAWESI_BBOX)

    def test_jsonld_geoshape_box(self):
        soup = _load_soup("article_jsonld_geoshape.html")
        geom, label = extract_geometry_from_html(soup)
        self.assertEqual(label, "schema.org JSON-LD")
        self.assertEnvelope(geom, 11.27, 51.36, 14.77, 53.56)

    def test_jsonld_multipoint_wrapped_as_collection(self):
        # Regression: Django's MultiPoint subclasses GeometryCollection, so a
        # naive isinstance check left it unwrapped and a GeometryCollectionField
        # save would fail with "Geometry type (MultiPoint) does not match
        # column type (GeometryCollection)".
        soup = _load_soup("article_jsonld_multipoint.html")
        geom, label = extract_geometry_from_html(soup)
        self.assertEqual(label, "schema.org JSON-LD")
        self.assertEqual(geom.geom_type, "GeometryCollection")
        self.assertEqual(geom[0].geom_type, "MultiPoint")
        self.assertEqual(len(geom[0]), 4)

    def test_jsonld_geocoordinates_point(self):
        soup = _load_soup("article_jsonld_geocoordinates.html")
        geom, label = extract_geometry_from_html(soup)
        self.assertEqual(label, "schema.org JSON-LD")
        # GeometryCollection wrapping a single point — coords are at the lon/lat
        self.assertEqual(geom.geom_type, "GeometryCollection")
        self.assertEqual(geom[0].geom_type, "Point")
        self.assertAlmostEqual(geom[0].x, 13.405)
        self.assertAlmostEqual(geom[0].y, 52.52)

    @responses.activate
    def test_geojson_link_branch(self):
        href = "http://janeway.test/dqj/plugins/geometadata/download/article/53/geojson/"
        body = (JANEWAY_FIXTURES / "article_geojson_link.geojson").read_text()
        responses.add(responses.GET, href, body=body, content_type="application/geo+json", status=200)
        soup = _load_soup("article_geojson_link.html")
        # base_url forces the relative href in the fixture to resolve to the mocked absolute URL
        geom, label = extract_geometry_from_html(
            soup,
            base_url="http://janeway.test/dqj/article/id/53/",
        )
        self.assertEqual(label, "link rel=alternate geo+json")
        self.assertEnvelope(geom, *self.SULAWESI_BBOX)
        # confirm the absolute URL was used (not the relative href)
        self.assertEqual(responses.calls[0].request.url, href)

    @responses.activate
    def test_geojson_link_falls_through_on_http_error(self):
        # The fixture has the link AND a DC.SpatialCoverage fallback.
        href = "https://example.invalid/feature_collection.geojson"
        responses.add(responses.GET, href, status=500)
        soup = _load_soup("article_link_and_dc.html")
        geom, label = extract_geometry_from_html(soup)
        self.assertEqual(label, "DC.SpatialCoverage")
        # Holy Roman Empire bbox from the DC.SpatialCoverage feature
        self.assertEnvelope(geom, 5.0, 46.0, 17.0, 55.0)

    @responses.activate
    def test_geojson_link_falls_through_on_malformed_body(self):
        href = "https://example.invalid/feature_collection.geojson"
        responses.add(responses.GET, href, body="not json", content_type="application/geo+json", status=200)
        soup = _load_soup("article_link_and_dc.html")
        geom, label = extract_geometry_from_html(soup)
        self.assertEqual(label, "DC.SpatialCoverage")

    def test_dc_spatial_coverage_feature(self):
        soup = _load_soup("article_dc_spatial.html")
        geom, label = extract_geometry_from_html(soup)
        self.assertEqual(label, "DC.SpatialCoverage")
        self.assertEnvelope(geom, 5.0, 46.0, 17.0, 55.0)

    def test_dc_box_polygon(self):
        soup = _load_soup("article_dc_box.html")
        geom, label = extract_geometry_from_html(soup)
        self.assertEqual(label, "DC.box")
        self.assertEnvelope(geom, 100.1, 13.9, 107.7, 22.5)

    def test_dc_box_rejects_non_wgs84(self):
        soup = _load_soup("article_dc_box_non_wgs84.html")
        geom, label = extract_geometry_from_html(soup)
        self.assertIsNone(geom)
        self.assertIsNone(label)

    @responses.activate
    def test_priority_jsonld_wins_over_other_signals(self):
        # The geo+json link target points to an invalid host and is mocked to fail
        # so we can also assert that, when JSON-LD wins, the link is not even fetched.
        responses.add(
            responses.GET,
            "https://example.invalid/should-not-be-fetched.geojson",
            status=500,
        )
        soup = _load_soup("article_all_signals.html")
        geom, label = extract_geometry_from_html(soup)
        self.assertEqual(label, "schema.org JSON-LD")
        self.assertEnvelope(geom, *self.SULAWESI_BBOX)
        self.assertEqual(len(responses.calls), 0, "geo+json link should not be fetched when JSON-LD matches first")

    @responses.activate
    def test_priority_geojson_link_beats_dc_when_no_jsonld(self):
        href = "https://example.invalid/feature_collection.geojson"
        body = (JANEWAY_FIXTURES / "article_geojson_link.geojson").read_text()
        responses.add(responses.GET, href, body=body, content_type="application/geo+json", status=200)
        soup = _load_soup("article_link_and_dc.html")
        geom, label = extract_geometry_from_html(soup)
        self.assertEqual(label, "link rel=alternate geo+json")
        self.assertEnvelope(geom, *self.SULAWESI_BBOX)

    def test_temporal_jsonld_open_start(self):
        soup = _load_soup("article_jsonld.html")
        starts, ends = extract_timeperiod_from_html(soup)
        self.assertEqual(starts, [None])
        self.assertEqual(ends, ["2024-12-31"])

    def test_temporal_jsonld_closed_interval(self):
        soup = _load_soup("article_jsonld_geoshape.html")
        starts, ends = extract_timeperiod_from_html(soup)
        self.assertEqual(starts, ["2008-01-01"])
        self.assertEqual(ends, ["2018-12-31"])

    def test_temporal_jsonld_single_instant(self):
        soup = _load_soup("article_jsonld_geocoordinates.html")
        starts, ends = extract_timeperiod_from_html(soup)
        self.assertEqual(starts, ["2020-01-01"])
        self.assertEqual(ends, ["2020-01-01"])

    def test_temporal_dc_temporal_only(self):
        # Isolated DC.temporal source — no DC.PeriodOfTime, no JSON-LD.
        soup = _load_soup("article_dc_temporal_only.html")
        starts, ends = extract_timeperiod_from_html(soup)
        self.assertEqual(starts, ["1900-01-01"])
        self.assertEqual(ends, ["1999-12-31"])

    def test_temporal_dc_periodoftime_only(self):
        # Isolated DC.PeriodOfTime source — no DC.temporal, no JSON-LD.
        soup = _load_soup("article_dc_periodoftime_only.html")
        starts, ends = extract_timeperiod_from_html(soup)
        self.assertEqual(starts, ["2000-06-01"])
        self.assertEqual(ends, ["2000-06-30"])

    def test_temporal_dc_open_start(self):
        # Both DC.temporal and DC.PeriodOfTime present, both with an open start.
        soup = _load_soup("article_dc_temporal_open.html")
        starts, ends = extract_timeperiod_from_html(soup)
        self.assertEqual(starts, [None])
        self.assertEqual(ends, ["2024-12-31"])

    def test_temporal_priority_jsonld_wins_over_dc(self):
        # JSON-LD must take precedence; the DC values are deliberately a
        # different range so a regression that picked DC instead would fail.
        soup = _load_soup("article_temporal_priority.html")
        starts, ends = extract_timeperiod_from_html(soup)
        self.assertEqual(starts, ["2024-01-01"])
        self.assertEqual(ends, ["2024-12-31"])

    def test_temporal_missing(self):
        soup = _load_soup("article_dc_box.html")
        starts, ends = extract_timeperiod_from_html(soup)
        self.assertEqual(starts, [None])
        self.assertEqual(ends, [None])
