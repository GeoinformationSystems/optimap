# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Browser test for the admin-configurable base-map layer switcher (issue #10).

works/static/js/map-base.js builds a Leaflet L.control.layers from the
BaseMapLayer rows seeded by migration 0036_basemaplayer_seed (OpenStreetMap
default, CARTO, Esri World Imagery, OpenTopoMap all enabled by default). This
drives a real browser to confirm a visitor can open the layer control and
switch the active base layer — something the Django-test-client-based
tests/test_basemaps.py cannot exercise since it never runs the Leaflet JS.
"""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from helium import get_driver, kill_browser, start_chrome
from selenium.webdriver.common.by import By

from works.models import BaseMapLayer


class BasemapSwitcherTests(StaticLiveServerTestCase):
    def setUp(self):
        # Migration 0036_basemaplayer_seed only runs once at `migrate` time;
        # `flush` (run after every TransactionTestCase, e.g. tests-ui/test_ogcapi.py's
        # OgcApiEndpointTest) truncates the table without re-seeding it, so a
        # prior TransactionTestCase in the same process can silently wipe these
        # rows. Ensure them here so this test doesn't depend on run order.
        BaseMapLayer.objects.get_or_create(
            provider_key="OpenStreetMap.Mapnik",
            defaults={"label": "OpenStreetMap", "enabled": True, "is_default": True, "order": 0},
        )
        BaseMapLayer.objects.get_or_create(
            provider_key="CartoDB.Voyager",
            defaults={"label": "CARTO (English labels)", "enabled": True, "is_default": False, "order": 1},
        )
        BaseMapLayer.objects.get_or_create(
            provider_key="Esri.WorldImagery",
            defaults={
                "label": "Esri World Imagery (satellite)",
                "enabled": True,
                "is_default": False,
                "order": 2,
            },
        )
        BaseMapLayer.objects.get_or_create(
            provider_key="OpenTopoMap",
            defaults={"label": "OpenTopoMap (topographic)", "enabled": True, "is_default": False, "order": 3},
        )
        # optimap.context_processors.basemap_settings caches the enabled-layers
        # list under this key for 5 minutes; an earlier test (e.g. one that ran
        # before the rows above existed) can leave a stale/empty value that
        # would otherwise outlive this test entirely.
        cache.delete("optimap_basemaps")

    def test_switch_base_layer(self):
        try:
            start_chrome(f"{self.live_server_url}/", headless=True)
            driver = get_driver()

            toggle = driver.find_element(By.CSS_SELECTOR, ".leaflet-control-layers-toggle")
            toggle.click()

            radios = driver.find_elements(By.CSS_SELECTOR, ".leaflet-control-layers-base input[type=radio]")
            self.assertGreaterEqual(len(radios), 4, "expected the 4 default-enabled base layers")

            labels = [r.find_element(By.XPATH, "./..").text.strip() for r in radios]
            self.assertIn("OpenStreetMap", labels)
            self.assertIn("Esri World Imagery (satellite)", labels)
            self.assertIn("OpenTopoMap (topographic)", labels)

            checked_before = [r for r in radios if r.is_selected()]
            self.assertEqual(len(checked_before), 1, "exactly one base layer should be selected initially")
            initial_label = checked_before[0].find_element(By.XPATH, "./..").text.strip()
            self.assertEqual(initial_label, "OpenStreetMap", "OpenStreetMap is the seeded default")

            esri_radio = next(r for r in radios if "Esri World Imagery" in r.find_element(By.XPATH, "./..").text)
            esri_radio.click()

            checked_after = [r for r in radios if r.is_selected()]
            self.assertEqual(len(checked_after), 1)
            self.assertEqual(
                checked_after[0].find_element(By.XPATH, "./..").text.strip(),
                "Esri World Imagery (satellite)",
                "switching should check the new layer and uncheck the old one",
            )

            get_driver().save_screenshot("tests-ui/screenshots/basemap_switcher.png")
        finally:
            kill_browser()
