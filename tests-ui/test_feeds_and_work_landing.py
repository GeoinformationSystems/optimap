# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""
UI tests for feeds pages and work landing pages.

Tests verify:
1. Feed pages load correctly with published works
2. Work landing pages display correctly
3. Navigation between feeds and works functions properly
"""

import os
import shutil
import tempfile
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.management import call_command
from django.test import override_settings
from helium import (
    S,
    find_all,
    get_driver,
    kill_browser,
    start_chrome,
    wait_until,
)

from works.models import Work

# Tiny fixture geojson files copied from tests/fixtures/global_regions/ keep
# load_global_regions offline during the UI run.
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "global_regions"


def _install_global_region_fixtures(target_dir):
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_DIR / "world_continents.geojson", target / "world_continents.geojson")
    shutil.copy(FIXTURE_DIR / "goas_v01_simplified.geojson", target / "goas_v01_simplified.geojson")
    (target / "goas_v01.gpkg").touch()


class FeedsAndWorkLandingTests(StaticLiveServerTestCase):
    """UI tests for feeds and work landing pages.

    Uses StaticLiveServerTestCase to automatically start a live test server
    that serves both the application and static files.
    """

    fixtures = ["test_data_optimap.json"]

    @classmethod
    def setUpClass(cls):
        """Set up class-level resources including live server."""
        cls._regions_tmp = tempfile.mkdtemp(prefix="optimap_global_regions_ui_")
        _install_global_region_fixtures(cls._regions_tmp)
        cls._settings_override = override_settings(GLOBAL_REGIONS_DATA_DIR=cls._regions_tmp)
        cls._settings_override.enable()
        super().setUpClass()

        call_command("load_global_regions")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._settings_override.disable()
        shutil.rmtree(cls._regions_tmp, ignore_errors=True)

    def test_europe_feed_page_loads(self):
        """Test that the Europe feed page loads and displays works."""
        try:
            start_chrome(f"{self.live_server_url}/feeds/continent/europe/", headless=True)
            driver = get_driver()

            # Check page loaded
            self.assertIn("Europe", driver.title)

            # Check for feed page content
            page_text = driver.page_source.lower()
            self.assertTrue("europe" in page_text and "feed" in page_text, "Page should contain 'Europe' and 'feed'")

            # The map container is static markup (not async-rendered), but
            # start_chrome() can return before Selenium's DOM/element lookup
            # is ready for this heavier page (SEO meta tags, JSON-LD, etc.) —
            # wait for it instead of racing it.
            wait_until(lambda: S("#feed-map").exists(), timeout_secs=10)
            self.assertTrue(S("#feed-map").exists(), "Page should have a map")

            # Take screenshot
            screenshot_path = os.path.join(os.getcwd(), "tests-ui", "screenshots", "europe_feed.png")
            driver.save_screenshot(screenshot_path)

        finally:
            kill_browser()

    def test_feeds_api_endpoint(self):
        """Test that the global GeoRSS API feed endpoint is accessible."""
        try:
            start_chrome(f"{self.live_server_url}/api/v1/feeds/optimap-global.rss", headless=True)
            driver = get_driver()

            # Check that page loaded (even if browser renders XML/RSS)
            self.assertIsNotNone(driver.page_source, "Page should have loaded")

            # Check the page is not a 404
            page_text = driver.page_source.lower()
            self.assertNotIn("page not found", page_text, "Should not be a 404 page")
            self.assertNotIn("error", page_text, "Should not be an error page")

            # Take screenshot
            screenshot_path = os.path.join(os.getcwd(), "tests-ui", "screenshots", "global_feed_api.png")
            driver.save_screenshot(screenshot_path)

        finally:
            kill_browser()

    def test_work_landing_page_from_fixture(self):
        """Test that a work landing page loads correctly using fixture data."""
        # Get first published work from fixture
        work = Work.objects.filter(status="p").first()

        try:
            # Use the work's identifier (DOI or ID)
            identifier = work.get_identifier()
            start_chrome(f"{self.live_server_url}/work/{identifier}/", headless=True)
            driver = get_driver()

            # Check page loaded
            self.assertIn("OPTIMAP", driver.title)

            # Check for work details on the page
            page_text = driver.page_source
            self.assertTrue(work.title in page_text, "Work landing page shows the work title")
            self.assertTrue(work.abstract in page_text, "Work landing page shows the work abstract")
            self.assertTrue(
                work.doi in page_text and f'href="https://doi.org/{work.doi}"',
                "Work landing page shows the work doi as a link",
            )
            self.assertTrue(
                work.source.name in page_text and f'href="{work.source.homepage_url}"' in page_text,
                "Work landing page shows the work source as a link",
            )
            self.assertTrue(
                f'href="{work.openalex_id}"' in page_text, "Work landing page shows the OpenAlex ID as a link"
            )

            leaflet_paths = find_all(S("path.leaflet-interactive"))
            self.assertEqual(len(leaflet_paths), 1)  # has one on the map

            # Take screenshot
            screenshot_path = os.path.join(os.getcwd(), "tests-ui", "screenshots", "work_landing.png")
            driver.save_screenshot(screenshot_path)
        finally:
            kill_browser()

    def test_work_landing_page_with_doi(self):
        """Test that a work landing page can be accessed via DOI."""
        # Get a work with DOI from fixture
        work = Work.objects.filter(status="p", doi__isnull=False).first()

        if not work:
            self.skipTest("No published works with DOI in fixtures")

        try:
            # Access via DOI
            start_chrome(f"{self.live_server_url}/work/{work.doi}/", headless=True)
            driver = get_driver()

            # Check page loaded
            self.assertIn("OPTIMAP", driver.title)

            # Check DOI is displayed
            self.assertTrue(work.doi in driver.page_source, f"Page should display DOI: {work.doi}")

            # Take screenshot
            screenshot_path = os.path.join(os.getcwd(), "tests-ui", "screenshots", "work_landing_doi.png")
            driver.save_screenshot(screenshot_path)

        finally:
            kill_browser()

    def test_work_landing_page_without_doi(self):
        """Test that a work landing page can be accessed via ID when no DOI."""
        # Get a work without DOI from fixture
        work = Work.objects.filter(status="p", doi__isnull=True).first()

        if not work:
            # If all works have DOI, just test with ID instead
            work = Work.objects.filter(status="p").first()
            if not work:
                self.skipTest("No published works in fixtures")

        try:
            # Access via internal ID
            start_chrome(f"{self.live_server_url}/work/{work.id}/", headless=True)
            driver = get_driver()

            # Check page loaded
            self.assertIn("OPTIMAP", driver.title)

            # Check work title is displayed
            self.assertTrue(work.title in driver.page_source, f"Page should display work title: {work.title}")

            # Take screenshot
            screenshot_path = os.path.join(os.getcwd(), "tests-ui", "screenshots", "work_landing_id.png")
            driver.save_screenshot(screenshot_path)

        finally:
            kill_browser()

    def test_feeds_listing_page(self):
        """Test that the feeds listing page loads and shows available feeds."""
        try:
            start_chrome(f"{self.live_server_url}/feeds/", headless=True)
            driver = get_driver()

            # Check page loaded
            self.assertIn("OPTIMAP", driver.title)

            # Check for feeds page content
            page_text = driver.page_source.lower()
            self.assertTrue(
                "feed" in page_text or "rss" in page_text or "atom" in page_text,
                "Page should mention feeds, RSS, or Atom",
            )

            # Take screenshot
            screenshot_path = os.path.join(os.getcwd(), "tests-ui", "screenshots", "feeds_listing.png")
            driver.save_screenshot(screenshot_path)

        finally:
            kill_browser()
