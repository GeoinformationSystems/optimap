# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import os

import django
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import GeometryCollection, Polygon
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from helium import *

from works.models import Work

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

User = get_user_model()


class SimpleTest(StaticLiveServerTestCase):
    """UI tests for the map page.

    Uses StaticLiveServerTestCase to automatically start a live test server
    that serves both the application and static files.
    """

    # Load test data fixtures into the test database
    fixtures = ["test_data_optimap.json"]

    @classmethod
    def setUpClass(cls):
        """Set up class-level resources including live server."""
        super().setUpClass()

    def setUp(self):
        """Set up for each test."""
        pass

    def test_map_page(self):
        """Test that the map page loads and displays geometries correctly."""
        # Use self.live_server_url which is automatically provided by StaticLiveServerTestCase
        start_chrome(f"{self.live_server_url}/", headless=True)

        get_driver().save_screenshot(r"tests-ui/screenshots/map.png")

        self.assertTrue(S("#map").exists())

        # Geometries are drawn after an async fetch('/api/v1/works/...') in
        # main.js, not on initial page load — wait for it instead of racing it.
        # 4 paths for 3 published works in test_data_optimap.json: "The First
        # Article" carries a GeometryCollection of a Point *and* a Polygon, and
        # Leaflet draws each sub-geometry as its own <path> (a CircleMarker's
        # circle is an SVG path too), so that one work contributes 2 paths.
        wait_until(lambda: len(find_all(S("path.leaflet-interactive"))) >= 4, timeout_secs=10)
        leaflet_paths = find_all(S("path.leaflet-interactive"))
        self.assertEqual(len(leaflet_paths), 4)
        for path in leaflet_paths:
            self.assertEqual(path.web_element.get_attribute("stroke"), "#158F9B")

        click(leaflet_paths[0])

        wait_until(lambda: Text("View work details").exists())

        # the last paper is the first in the paths
        self.assertIn("Visit work", S("div.leaflet-popup-content").web_element.text)
        self.assertIn("Paper 3", S("div.leaflet-popup-content").web_element.text)
        self.assertIn("OPTIMAP Test Journal", S("div.leaflet-popup-content").web_element.text)
        self.assertIn("better? Dresden!", S("div.leaflet-popup-content").web_element.text)

        get_driver().save_screenshot(r"tests-ui/screenshots/map_popup.png")

        # continue: click(link('Visit Article'))

        kill_browser()


class NoDoiPopupTest(StaticLiveServerTestCase):
    """Regression: works without a DOI must still expose a "View work details"
    button in the single-feature popup. ``GeoFeatureModelSerializer`` puts the
    primary key at ``feature.id`` (GeoJSON convention), not in
    ``feature.properties``, so the popup builder has to check both. Without
    that, no-DOI works (Mountain Wetlands, AGILE-GISS via OpenAlex source)
    rendered a popup with title + abstract but no link to the landing page.
    """

    def setUp(self):
        # Single published no-DOI work — keeps the map free of unrelated paths
        # so we can target the click reliably. We use a Polygon (not a Point)
        # because ``L.geoJSON`` renders points as default Markers (img tags)
        # while polygons render as ``path.leaflet-interactive`` SVG elements,
        # which the existing UI test already targets.
        polygon = Polygon(
            (
                (13.70, 51.02),
                (13.78, 51.02),
                (13.78, 51.08),
                (13.70, 51.08),
                (13.70, 51.02),
            )
        )
        self.work = Work.objects.create(
            title="No-DOI Map Popup Test",
            status="p",
            doi=None,
            url="http://example.org/no-doi",
            geometry=GeometryCollection(polygon),  # Dresden bbox
        )

    def test_map_does_not_scroll_beyond_world_bounds(self):
        """Map must not show duplicate Earths when panning far east (issue #129)."""
        start_chrome(f"{self.live_server_url}/", headless=True)
        try:
            self.assertTrue(S("#map").exists())
            wait_until(
                lambda: get_driver().execute_script("return typeof window._optimapMap !== 'undefined'"),
                timeout_secs=10,
            )
            get_driver().execute_script("window._optimapMap.panTo([0, 400]);")
            center_lng = get_driver().execute_script("return window._optimapMap.getCenter().lng;")
            self.assertLessEqual(center_lng, 180.0, "Map center exceeded 180° east")
            self.assertGreaterEqual(center_lng, -180.0, "Map center went below -180° west")
        finally:
            kill_browser()

    def test_view_details_button_links_to_id_url(self):
        start_chrome(f"{self.live_server_url}/", headless=True)
        try:
            self.assertTrue(S("#map").exists())

            # Wait for the async fetch from /api/v1/works/ to populate the
            # map — without this, the test can race the network round-trip
            # and find zero paths.
            wait_until(lambda: len(find_all(S("path.leaflet-interactive"))) >= 1)
            paths = find_all(S("path.leaflet-interactive"))
            self.assertEqual(len(paths), 1)
            click(paths[0])

            wait_until(lambda: Text("View work details").exists())
            popup = S("div.leaflet-popup-content").web_element
            # Title from properties is rendered.
            self.assertIn("No-DOI Map Popup Test", popup.text)
            # The button's href falls back to /work/<id>/ (the GeoJSON
            # ``feature.id``) because there is no DOI.
            link_el = popup.find_element("css selector", "a.btn-primary")
            self.assertTrue(link_el.get_attribute("href").endswith(f"/work/{self.work.id}/"))
        finally:
            kill_browser()
