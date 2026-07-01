# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for per-country GeoRSS/Atom feeds and GeoJSON/GeoPackage/CSV downloads (issue #52)."""

import json
import xml.etree.ElementTree as ET

from django.contrib.gis.geos import GeometryCollection, MultiPolygon, Point, Polygon
from django.test import TestCase
from django.urls import reverse

from works.models import Country, GlobalRegion, Work


def _square(cx, cy, d=1.0):
    return Polygon(((cx - d, cy - d), (cx + d, cy - d), (cx + d, cy + d), (cx - d, cy + d), (cx - d, cy - d)))


class _CountryFixtureMixin:
    """Shared setup for feed and download test classes."""

    @classmethod
    def setUpTestData(cls):
        cls.de = Country.objects.create(
            name="Germany",
            iso_code="DE",
            continent="Europe",
            geom=MultiPolygon(_square(10, 51)),
        )
        cls.fr = Country.objects.create(
            name="France",
            iso_code="FR",
            continent="Europe",
            geom=MultiPolygon(_square(2, 46)),
        )
        # Published work with geometry in Germany — appears in feed AND download.
        cls.pub_geo = Work.objects.create(
            title="German study with geometry",
            abstract="Abstract.",
            url="https://example.com/pub",
            doi="10.1234/pub",
            status="p",
            geometry=GeometryCollection(Point(10, 51)),
        )
        cls.pub_geo.countries.add(cls.de)
        # Published work without geometry in Germany — appears in download, not feed.
        cls.pub_no_geo = Work.objects.create(
            title="German study no geometry",
            url="https://example.com/no-geo",
            doi="10.1234/no-geo",
            status="p",
        )
        cls.pub_no_geo.countries.add(cls.de)
        # Harvested (unpublished) work in Germany — must never appear.
        cls.harv = Work.objects.create(
            title="German harvested",
            url="https://example.com/harv",
            doi="10.1234/harv",
            status="h",
            geometry=GeometryCollection(Point(10, 51)),
        )
        cls.harv.countries.add(cls.de)
        # Published work in France only — must not appear in Germany feed/downloads.
        cls.french = Work.objects.create(
            title="French study",
            url="https://example.com/fr",
            doi="10.1234/fr",
            status="p",
            geometry=GeometryCollection(Point(2, 46)),
        )
        cls.french.countries.add(cls.fr)


# ---------------------------------------------------------------------------
# Feed tests
# ---------------------------------------------------------------------------


class CountryFeedTests(_CountryFixtureMixin, TestCase):
    def _rss(self, slug="germany"):
        return f"/api/v1/feeds/country-{slug}.rss"

    def _atom(self, slug="germany"):
        return f"/api/v1/feeds/country-{slug}.atom"

    # --- status codes ---

    def test_rss_200_for_known_country(self):
        self.assertEqual(self.client.get(self._rss()).status_code, 200)

    def test_atom_200_for_known_country(self):
        self.assertEqual(self.client.get(self._atom()).status_code, 200)

    def test_rss_404_unknown_slug(self):
        self.assertEqual(self.client.get(self._rss("no-such-country")).status_code, 404)

    def test_atom_404_unknown_slug(self):
        self.assertEqual(self.client.get(self._atom("no-such-country")).status_code, 404)

    # --- content type ---

    def test_rss_content_type_is_xml(self):
        self.assertIn("xml", self.client.get(self._rss())["Content-Type"])

    def test_atom_content_type_is_xml(self):
        self.assertIn("xml", self.client.get(self._atom())["Content-Type"])

    # --- feed title contains country name ---

    def test_rss_title_contains_country_name(self):
        self.assertIn(b"Germany", self.client.get(self._rss()).content)

    def test_atom_title_contains_country_name(self):
        self.assertIn(b"Germany", self.client.get(self._atom()).content)

    # --- item inclusion / exclusion ---

    def test_rss_includes_published_work_with_geometry(self):
        self.assertIn(b"German study with geometry", self.client.get(self._rss()).content)

    def test_atom_includes_published_work_with_geometry(self):
        self.assertIn(b"German study with geometry", self.client.get(self._atom()).content)

    def test_rss_excludes_work_without_geometry(self):
        self.assertNotIn(b"German study no geometry", self.client.get(self._rss()).content)

    def test_rss_excludes_unpublished_work(self):
        self.assertNotIn(b"German harvested", self.client.get(self._rss()).content)

    def test_rss_excludes_other_country(self):
        self.assertNotIn(b"French study", self.client.get(self._rss()).content)

    def test_atom_excludes_other_country(self):
        self.assertNotIn(b"French study", self.client.get(self._atom()).content)

    # --- GeoRSS geometry ---

    def test_rss_contains_georss_point(self):
        self.assertIn(b"georss:point", self.client.get(self._rss()).content)

    def test_atom_contains_georss_point(self):
        self.assertIn(b"georss:point", self.client.get(self._atom()).content)

    # --- XML validity ---

    def test_rss_is_valid_xml(self):
        try:
            ET.fromstring(self.client.get(self._rss()).content)
        except ET.ParseError as exc:
            self.fail(f"RSS feed is not valid XML: {exc}")

    def test_atom_is_valid_xml(self):
        try:
            ET.fromstring(self.client.get(self._atom()).content)
        except ET.ParseError as exc:
            self.fail(f"Atom feed is not valid XML: {exc}")

    # --- URL reversal ---

    def test_rss_url_reverses_correctly(self):
        url = reverse("optimap:api-country-georss", kwargs={"country_slug": "germany"})
        self.assertEqual(url, "/api/v1/feeds/country-germany.rss")

    def test_atom_url_reverses_correctly(self):
        url = reverse("optimap:api-country-atom", kwargs={"country_slug": "germany"})
        self.assertEqual(url, "/api/v1/feeds/country-germany.atom")

    # --- ISO-code redirect ---

    def test_rss_iso_code_redirects_to_slug(self):
        resp = self.client.get("/api/v1/feeds/country-DE.rss")
        self.assertEqual(resp.status_code, 301)
        self.assertIn("germany", resp["Location"])

    def test_atom_iso_code_redirects_to_slug(self):
        resp = self.client.get("/api/v1/feeds/country-DE.atom")
        self.assertEqual(resp.status_code, 301)
        self.assertIn("germany", resp["Location"])

    def test_rss_lowercase_iso_redirects(self):
        resp = self.client.get("/api/v1/feeds/country-de.rss")
        self.assertEqual(resp.status_code, 301)
        self.assertIn("germany", resp["Location"])

    def test_rss_iso_redirect_preserves_now_param(self):
        resp = self.client.get("/api/v1/feeds/country-DE.rss?now=1")
        self.assertEqual(resp.status_code, 301)
        self.assertIn("now=1", resp["Location"])

    def test_atom_iso_redirect_preserves_now_param(self):
        resp = self.client.get("/api/v1/feeds/country-DE.atom?now=1")
        self.assertEqual(resp.status_code, 301)
        self.assertIn("now=1", resp["Location"])


# ---------------------------------------------------------------------------
# Download tests
# ---------------------------------------------------------------------------


class CountryDownloadTests(_CountryFixtureMixin, TestCase):
    def _url(self, fmt, slug="germany"):
        return reverse(f"optimap:download-country-{fmt}", kwargs={"country_slug": slug})

    # --- status codes ---

    def test_geojson_200_for_known_country(self):
        self.assertEqual(self.client.get(self._url("geojson")).status_code, 200)

    def test_gpkg_200_for_known_country(self):
        self.assertEqual(self.client.get(self._url("gpkg")).status_code, 200)

    def test_csv_200_for_known_country(self):
        self.assertEqual(self.client.get(self._url("csv")).status_code, 200)

    def test_geojson_404_unknown_slug(self):
        self.assertEqual(self.client.get(self._url("geojson", "no-such")).status_code, 404)

    def test_gpkg_404_unknown_slug(self):
        self.assertEqual(self.client.get(self._url("gpkg", "no-such")).status_code, 404)

    def test_csv_404_unknown_slug(self):
        self.assertEqual(self.client.get(self._url("csv", "no-such")).status_code, 404)

    # --- content type ---

    def test_geojson_content_type(self):
        self.assertIn("application/geo+json", self.client.get(self._url("geojson"))["Content-Type"])

    def test_gpkg_content_type(self):
        self.assertIn("geopackage", self.client.get(self._url("gpkg"))["Content-Type"])

    def test_csv_content_type(self):
        self.assertIn("text/csv", self.client.get(self._url("csv"))["Content-Type"])

    # --- Content-Disposition filename ---

    def test_geojson_filename(self):
        self.assertIn("optimap_country_germany.geojson", self.client.get(self._url("geojson"))["Content-Disposition"])

    def test_gpkg_filename(self):
        self.assertIn("optimap_country_germany.gpkg", self.client.get(self._url("gpkg"))["Content-Disposition"])

    def test_csv_filename(self):
        self.assertIn("optimap_country_germany.csv", self.client.get(self._url("csv"))["Content-Disposition"])

    # --- GeoJSON content ---

    def test_geojson_is_valid_feature_collection(self):
        data = json.loads(self.client.get(self._url("geojson")).content)
        self.assertEqual(data["type"], "FeatureCollection")
        self.assertIn("features", data)

    def test_geojson_includes_published_work_with_geometry(self):
        body = self.client.get(self._url("geojson")).content.decode()
        self.assertIn("German study with geometry", body)

    def test_geojson_includes_published_work_without_geometry(self):
        # Downloads include all published works; geometry is optional.
        body = self.client.get(self._url("geojson")).content.decode()
        self.assertIn("German study no geometry", body)

    def test_geojson_excludes_unpublished_work(self):
        body = self.client.get(self._url("geojson")).content.decode()
        self.assertNotIn("German harvested", body)

    def test_geojson_excludes_other_country(self):
        body = self.client.get(self._url("geojson")).content.decode()
        self.assertNotIn("French study", body)

    # --- GeoPackage / CSV basic sanity ---

    def test_gpkg_body_is_non_empty(self):
        self.assertGreater(len(self.client.get(self._url("gpkg")).content), 0)

    def test_csv_body_is_non_empty(self):
        self.assertGreater(len(self.client.get(self._url("csv")).content), 0)

    # --- cache bypass ---

    def test_now_param_returns_200(self):
        data = json.loads(self.client.get(self._url("geojson") + "?now").content)
        self.assertEqual(data["type"], "FeatureCollection")

    # --- ISO-code redirect ---

    def test_geojson_iso_code_redirects_to_slug(self):
        resp = self.client.get("/api/v1/countries/DE/download/geojson/")
        self.assertEqual(resp.status_code, 301)
        self.assertIn("germany", resp["Location"])

    def test_gpkg_iso_code_redirects_to_slug(self):
        resp = self.client.get("/api/v1/countries/DE/download/gpkg/")
        self.assertEqual(resp.status_code, 301)
        self.assertIn("germany", resp["Location"])

    def test_csv_iso_code_redirects_to_slug(self):
        resp = self.client.get("/api/v1/countries/DE/download/csv/")
        self.assertEqual(resp.status_code, 301)
        self.assertIn("germany", resp["Location"])

    def test_geojson_iso_redirect_preserves_now_param(self):
        resp = self.client.get("/api/v1/countries/DE/download/geojson/?now")
        self.assertEqual(resp.status_code, 301)
        self.assertIn("now", resp["Location"])

    def test_gpkg_iso_redirect_preserves_now_param(self):
        resp = self.client.get("/api/v1/countries/DE/download/gpkg/?now")
        self.assertEqual(resp.status_code, 301)
        self.assertIn("now", resp["Location"])

    def test_csv_iso_redirect_preserves_now_param(self):
        resp = self.client.get("/api/v1/countries/DE/download/csv/?now")
        self.assertEqual(resp.status_code, 301)
        self.assertIn("now", resp["Location"])


# ---------------------------------------------------------------------------
# Country page — feeds & downloads card
# ---------------------------------------------------------------------------


class CountryPageFeedsCardTests(_CountryFixtureMixin, TestCase):
    def _page(self, slug="germany"):
        return reverse("optimap:at-place", kwargs={"place_slug": slug})

    def test_country_page_has_rss_autodiscovery(self):
        body = self.client.get(self._page()).content.decode()
        self.assertIn("application/rss+xml", body)
        self.assertIn("/api/v1/feeds/country-germany.rss", body)

    def test_country_page_has_atom_autodiscovery(self):
        body = self.client.get(self._page()).content.decode()
        self.assertIn("application/atom+xml", body)
        self.assertIn("/api/v1/feeds/country-germany.atom", body)

    def test_country_page_shows_feeds_and_downloads_card(self):
        body = self.client.get(self._page()).content.decode()
        self.assertIn("Feeds &amp; downloads", body)
        self.assertIn("/api/v1/feeds/country-germany.rss", body)
        self.assertIn("/api/v1/feeds/country-germany.atom", body)
        self.assertIn("/api/v1/countries/germany/download/geojson/", body)
        self.assertIn("/api/v1/countries/germany/download/gpkg/", body)
        self.assertIn("/api/v1/countries/germany/download/csv/", body)

    def test_country_page_card_uses_country_name_not_generic(self):
        """The card prose shows the country name, not the generic placeholder."""
        body = self.client.get(self._page()).content.decode()
        self.assertIn("Germany", body)
        self.assertNotIn("this country", body)

    def test_continent_page_does_not_show_country_feeds_card(self):
        europe = GlobalRegion.objects.create(
            name="Europe",
            region_type=GlobalRegion.CONTINENT,
            source_url="https://example.org",
            license="x",
            geom=MultiPolygon(_square(10, 50, 30)),
        )
        resp = self.client.get(reverse("optimap:at-place", kwargs={"place_slug": europe.get_slug()}))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn("country-", body.split("Feeds")[0] if "Feeds" in body else body)
        # Specifically: no country feed URLs should appear.
        self.assertNotIn("/api/v1/feeds/country-", body)
        self.assertNotIn("/api/v1/countries/", body)
