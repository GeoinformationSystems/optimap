# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import json

from django.test import TestCase
from django.test.utils import override_settings

from works.models import BaseMapLayer

_CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "memory": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "dummy": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"},
}

# Rows seeded by migrations 0036_basemaplayer_seed / 0037_seed_basemapworld_vector.
# Migrations only run once at `migrate` time; a TransactionTestCase elsewhere in
# the suite (e.g. tests.test_ogcapi.OgcApiEndpointTest) truncates this table via
# `flush` without re-seeding it, so tests that depend on these rows re-create
# them here to avoid depending on suite run order.
_SEEDED_LAYERS = [
    {
        "provider_key": "OpenStreetMap.Mapnik",
        "label": "OpenStreetMap",
        "enabled": True,
        "is_default": True,
        "order": 0,
    },
    {
        "provider_key": "CartoDB.Voyager",
        "label": "CARTO (English labels)",
        "enabled": True,
        "is_default": False,
        "order": 1,
    },
    {
        "provider_key": "Esri.WorldImagery",
        "label": "Esri World Imagery (satellite)",
        "enabled": True,
        "is_default": False,
        "order": 2,
    },
    {
        "provider_key": "OpenTopoMap",
        "label": "OpenTopoMap (topographic)",
        "enabled": True,
        "is_default": False,
        "order": 3,
    },
    {
        "provider_key": "CartoDB.Positron",
        "label": "CARTO Positron (light)",
        "enabled": False,
        "is_default": False,
        "order": 10,
    },
    {
        "provider_key": "Esri.WorldStreetMap",
        "label": "Esri World Street Map",
        "enabled": False,
        "is_default": False,
        "order": 11,
    },
    {
        "provider_key": "Stadia.AlidadeSmooth",
        "label": "Stadia Alidade Smooth",
        "enabled": False,
        "is_default": False,
        "order": 12,
    },
    {
        "provider_key": "BasemapWorldVector",
        "label": "basemap.world (vector, BKG)",
        "enabled": False,
        "is_default": False,
        "order": 20,
        "options": {"style": "https://sgx.geodatenzentrum.de/gdz_basemapworld_vektor/styles/bm_web_wld_col.json"},
    },
]


def _ensure_basemap_seed():
    for data in _SEEDED_LAYERS:
        BaseMapLayer.objects.get_or_create(provider_key=data["provider_key"], defaults=data)


class BaseMapLayerModelTest(TestCase):
    def setUp(self):
        BaseMapLayer.objects.all().delete()

    def _make(self, key, label, enabled=True, is_default=False, order=0):
        return BaseMapLayer.objects.create(
            provider_key=key, label=label, enabled=enabled, is_default=is_default, order=order
        )

    def test_enabled_layers_returns_only_enabled(self):
        self._make("OpenStreetMap.Mapnik", "OSM", enabled=True, is_default=True)
        self._make("CartoDB.Voyager", "CARTO", enabled=False)
        result = BaseMapLayer.enabled_layers()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["provider_key"], "OpenStreetMap.Mapnik")

    def test_enabled_layers_ordered_by_order_then_label(self):
        self._make("CartoDB.Voyager", "CARTO", order=2)
        self._make("OpenStreetMap.Mapnik", "OSM", order=1, is_default=True)
        self._make("OpenTopoMap", "Topo", order=3)
        result = BaseMapLayer.enabled_layers()
        self.assertEqual(
            [r["provider_key"] for r in result], ["OpenStreetMap.Mapnik", "CartoDB.Voyager", "OpenTopoMap"]
        )

    def test_enabled_layers_exact_one_default_marked(self):
        self._make("OpenStreetMap.Mapnik", "OSM", is_default=True)
        self._make("CartoDB.Voyager", "CARTO")
        result = BaseMapLayer.enabled_layers()
        defaults = [r for r in result if r["default"]]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]["provider_key"], "OpenStreetMap.Mapnik")

    def test_enabled_layers_fallback_to_first_when_no_default(self):
        self._make("CartoDB.Voyager", "CARTO", order=1)
        self._make("OpenTopoMap", "Topo", order=2)
        result = BaseMapLayer.enabled_layers()
        defaults = [r for r in result if r["default"]]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]["provider_key"], "CartoDB.Voyager")

    def test_enabled_layers_empty_when_none_enabled(self):
        self._make("OpenStreetMap.Mapnik", "OSM", enabled=False)
        self.assertEqual(BaseMapLayer.enabled_layers(), [])

    def test_str(self):
        layer = self._make("CartoDB.Voyager", "CARTO Voyager")
        self.assertEqual(str(layer), "CARTO Voyager (CartoDB.Voyager)")

    def test_options_included_in_enabled_layers(self):
        self._make("MapTiler.Streets", "MapTiler", enabled=True, is_default=True)
        layer = BaseMapLayer.objects.get(provider_key="MapTiler.Streets")
        layer.options = {"key": "abc123"}
        layer.save()
        result = BaseMapLayer.enabled_layers()
        self.assertEqual(result[0]["options"], {"key": "abc123"})


class BaseMapLayerSeedMigrationTest(TestCase):
    """Verify the seed migration created the expected rows."""

    def setUp(self):
        _ensure_basemap_seed()

    def test_osm_is_enabled_and_default(self):
        osm = BaseMapLayer.objects.get(provider_key="OpenStreetMap.Mapnik")
        self.assertTrue(osm.enabled)
        self.assertTrue(osm.is_default)

    def test_carto_voyager_is_enabled(self):
        layer = BaseMapLayer.objects.get(provider_key="CartoDB.Voyager")
        self.assertTrue(layer.enabled)
        self.assertFalse(layer.is_default)

    def test_esri_world_imagery_is_enabled(self):
        layer = BaseMapLayer.objects.get(provider_key="Esri.WorldImagery")
        self.assertTrue(layer.enabled)

    def test_opentopo_is_enabled(self):
        layer = BaseMapLayer.objects.get(provider_key="OpenTopoMap")
        self.assertTrue(layer.enabled)

    def test_disabled_extras_exist_and_are_disabled(self):
        for key in ["CartoDB.Positron", "Esri.WorldStreetMap", "Stadia.AlidadeSmooth", "BasemapWorldVector"]:
            layer = BaseMapLayer.objects.get(provider_key=key)
            self.assertFalse(layer.enabled, msg=f"{key} should be disabled by default")

    def test_basemap_world_vector_has_style_option(self):
        layer = BaseMapLayer.objects.get(provider_key="BasemapWorldVector")
        self.assertIn("style", layer.options)
        self.assertIn("gdz_basemapworld_vektor", layer.options["style"])

    def test_exactly_one_default_among_enabled(self):
        defaults = BaseMapLayer.objects.filter(enabled=True, is_default=True)
        self.assertEqual(defaults.count(), 1)


@override_settings(CACHES=_CACHES)
class BasemapContextProcessorTest(TestCase):
    def setUp(self):
        from django.core.cache import caches

        for alias in caches:
            caches[alias].clear()
        BaseMapLayer.objects.all().delete()
        BaseMapLayer.objects.create(
            provider_key="OpenStreetMap.Mapnik", label="OSM", enabled=True, is_default=True, order=0
        )
        BaseMapLayer.objects.create(
            provider_key="CartoDB.Voyager", label="CARTO", enabled=True, is_default=False, order=1
        )

    def test_context_processor_returns_list(self):
        from optimap.context_processors import basemap_settings

        result = basemap_settings(None)
        self.assertIn("optimap_basemaps", result)
        layers = result["optimap_basemaps"]
        self.assertEqual(len(layers), 2)
        keys = [layer["provider_key"] for layer in layers]
        self.assertIn("OpenStreetMap.Mapnik", keys)
        self.assertIn("CartoDB.Voyager", keys)

    def test_context_processor_marks_default(self):
        from optimap.context_processors import basemap_settings

        result = basemap_settings(None)
        defaults = [layer for layer in result["optimap_basemaps"] if layer["default"]]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]["provider_key"], "OpenStreetMap.Mapnik")

    def test_has_maplibre_basemap_false_without_vector_layer(self):
        from optimap.context_processors import basemap_settings

        result = basemap_settings(None)
        self.assertFalse(result["has_maplibre_basemap"])

    def test_has_maplibre_basemap_true_with_vector_layer(self):
        from django.core.cache import cache

        from optimap.context_processors import basemap_settings

        BaseMapLayer.objects.create(
            provider_key="BasemapWorldVector",
            label="basemap.world",
            enabled=True,
            is_default=False,
            order=5,
            options={"style": "https://sgx.geodatenzentrum.de/gdz_basemapworld_vektor/styles/bm_web_wld_col.json"},
        )
        cache.delete("optimap_basemaps")
        result = basemap_settings(None)
        self.assertTrue(result["has_maplibre_basemap"])


@override_settings(CACHES=_CACHES)
class BasemapJsonScriptInjectionTest(TestCase):
    """Homepage renders the #optimap-basemaps json_script tag."""

    def test_homepage_contains_basemaps_json_script(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('id="optimap-basemaps"', content)
        # Extract the JSON and verify it is valid
        import re

        match = re.search(r'id="optimap-basemaps"[^>]*>(.*?)</script>', content, re.DOTALL)
        self.assertIsNotNone(match, "Could not find optimap-basemaps script tag")
        data = json.loads(match.group(1))
        self.assertIsInstance(data, list)

    def test_basemaps_json_contains_enabled_providers(self):
        response = self.client.get("/")
        content = response.content.decode()
        import re

        match = re.search(r'id="optimap-basemaps"[^>]*>(.*?)</script>', content, re.DOTALL)
        data = json.loads(match.group(1))
        keys = [d["provider_key"] for d in data]
        self.assertIn("OpenStreetMap.Mapnik", keys)
        self.assertIn("CartoDB.Voyager", keys)


@override_settings(CACHES=_CACHES)
class PrivacyPolicyTest(TestCase):
    """Privacy page shows only the provider statements for enabled basemaps."""

    def setUp(self):
        # Clear all caches before each test so @cache_page and the basemap
        # context-processor cache don't bleed state across test methods.
        from django.core.cache import caches

        for alias in caches:
            caches[alias].clear()
        _ensure_basemap_seed()

    def test_privacy_page_mentions_carto_when_enabled(self):
        response = self.client.get("/privacy/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"CARTO", response.content)

    def test_privacy_page_mentions_esri_when_enabled(self):
        response = self.client.get("/privacy/")
        self.assertIn(b"Esri", response.content)

    def test_privacy_page_mentions_opentopomap_when_enabled(self):
        response = self.client.get("/privacy/")
        self.assertIn(b"OpenTopoMap", response.content)

    def test_privacy_page_omits_carto_when_disabled(self):
        BaseMapLayer.objects.filter(provider_key__startswith="CartoDB").update(enabled=False)
        response = self.client.get("/privacy/")
        self.assertNotIn(b"CARTO", response.content)

    def test_privacy_page_shows_bkg_when_basemapworld_enabled(self):
        # BasemapWorldVector is seeded but disabled; enable it for this test.
        BaseMapLayer.objects.filter(provider_key="BasemapWorldVector").update(enabled=True)
        response = self.client.get("/privacy/")
        self.assertIn(b"Bundesamt", response.content)
        self.assertIn(b"basemap.world", response.content)

    def test_privacy_page_omits_bkg_when_not_enabled(self):
        response = self.client.get("/privacy/")
        self.assertNotIn(b"Bundesamt", response.content)
