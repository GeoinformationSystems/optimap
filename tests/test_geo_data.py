# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import csv
import gzip
import json
import os
import tempfile
from pathlib import Path

import fiona
from django.conf import settings
from django.core.serializers import serialize
from django.test import TestCase
from django.urls import reverse

from works.models import Collection, Source, Work
from works.tasks import (
    convert_geojson_to_geopackage,
    regenerate_all_data_dumps,
    regenerate_csv_cache,
    regenerate_geojson_cache,
    regenerate_geopackage_cache,
)
from works.views import generate_geopackage


class GeoDataAlternativeTestCase(TestCase):
    def setUp(self):
        Work.objects.all().delete()
        wkt_point1 = "GEOMETRYCOLLECTION(POINT(7.59573 51.96944))"
        wkt_point2 = "GEOMETRYCOLLECTION(POINT(8.59573 52.96944))"
        wkt_point3 = "GEOMETRYCOLLECTION(POINT(9.59573 53.96944))"

        s1 = Source.objects.create(name="Source One", url_field="http://example.com/1")
        Work.objects.create(
            title="Publication One",
            abstract="Abstract of publication one.",
            publicationDate="2020-01-01",
            url="http://example.com/1",
            source=s1,
            doi="10.0001/one",
            geometry=wkt_point1,
            timeperiod_startdate=["2020-01-01"],
            timeperiod_enddate=["2020-12-31"],
        )
        s2 = Source.objects.create(name="Source Two", url_field="http://example.com/2")
        Work.objects.create(
            title="Publication Two",
            abstract="Abstract of publication two.",
            publicationDate="2020-06-01",
            url="http://example.com/2",
            source=s2,
            doi="10.0001/two",
            geometry=wkt_point2,
            timeperiod_startdate=["2020-06-01"],
            timeperiod_enddate=["2020-12-31"],
        )
        s3 = Source.objects.create(name="Source Three", url_field="http://example.com/3")
        Work.objects.create(
            title="Publication Three",
            abstract="Abstract of publication three.",
            publicationDate="2020-09-01",
            url="http://example.com/3",
            source=s3,
            doi="10.0001/three",
            geometry=wkt_point3,
            timeperiod_startdate=["2020-09-01"],
            timeperiod_enddate=["2020-12-31"],
        )

    def test_geojson_generation(self):
        geojson_data = serialize("geojson", Work.objects.all(), geometry_field="geometry")
        self.assertTrue(len(geojson_data) > 0, "GeoJSON data should not be empty")
        geojson_obj = json.loads(geojson_data)
        self.assertEqual(geojson_obj.get("type"), "FeatureCollection", "GeoJSON type should be FeatureCollection")
        features = geojson_obj.get("features", [])
        self.assertEqual(len(features), Work.objects.count(), "Feature count should match Publication count")
        self.assertIn("title", features[0]["properties"], "Each feature should have a 'title' property")

    def test_geopackage_generation(self):
        gpkg_path = generate_geopackage()
        self.assertTrue(os.path.exists(gpkg_path), "GeoPackage file should exist")
        with fiona.open(gpkg_path, layer="works") as layer:
            features = list(layer)
            self.assertEqual(
                len(features), Work.objects.count(), "Feature count in GeoPackage should match the Publication count"
            )

    def test_update_reflects_in_generated_data(self):
        initial_geojson = serialize("geojson", Work.objects.all(), geometry_field="geometry")
        initial_obj = json.loads(initial_geojson)
        initial_title = initial_obj["features"][0]["properties"]["title"]
        pub = Work.objects.first()
        pub.title += " Updated"
        pub.save()

        updated_geojson = serialize("geojson", Work.objects.all(), geometry_field="geometry")
        updated_obj = json.loads(updated_geojson)
        updated_title = updated_obj["features"][0]["properties"]["title"]
        self.assertNotEqual(
            initial_title, updated_title, "The title of the first publication should update in the GeoJSON output"
        )

        # Test GeoPackage update
        initial_gpkg = open(generate_geopackage(), "rb").read()
        updated_gpkg = open(generate_geopackage(), "rb").read()
        self.assertNotEqual(initial_gpkg, updated_gpkg, "The GeoPackage data should update when a Publication changes")

    def test_data_endpoint(self):
        url = reverse("optimap:data")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, "Data endpoint should return status 200")
        content = response.content.decode()
        self.assertIn(f"Data dumps run every {settings.DATA_DUMP_INTERVAL_HOURS} hour", content)
        cache_dir = Path(tempfile.gettempdir()) / "optimap_cache"
        dumps = sorted(cache_dir.glob("optimap_data_dump_*.geojson"), reverse=True)
        self.assertTrue(dumps, "At least one data dump should exist for Last updated check")
        self.assertIn("Last updated:", content)

    def test_download_geojson_gzip(self):
        regenerate_geojson_cache()
        url = reverse("optimap:download_geojson")
        response = self.client.get(url, HTTP_ACCEPT_ENCODING="gzip")
        self.assertEqual(response.status_code, 200, "Gzip download should return status 200")
        self.assertEqual(response["Content-Encoding"], "gzip", "Response should be gzipped when requested")
        self.assertEqual(
            response["Content-Type"], "application/geo+json", "Content-Type should be application/geo+json"
        )
        self.assertRegex(response["Content-Disposition"], r"optimap_data_dump_.*\.geojson\.gz")

    def test_download_geopackage_endpoint(self):
        url = reverse("optimap:download_geopackage")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, "GeoPackage endpoint should return 200")
        self.assertEqual(
            response["Content-Type"],
            "application/geopackage+sqlite3",
            "Content-Type should be application/geopackage+sqlite3",
        )
        self.assertRegex(response["Content-Disposition"], r"optimap_data_dump_.*\.gpkg")

    def test_regenerate_geojson_cache_creates_files(self):
        cache_dir = Path(tempfile.gettempdir()) / "optimap_cache"
        for f in cache_dir.glob("optimap_data_dump_*"):
            f.unlink()

        returned_path = regenerate_geojson_cache()
        self.assertTrue(Path(returned_path).exists(), "GeoJSON cache file should be created")
        self.assertTrue(returned_path.endswith(".geojson"))

        gzip_path = Path(returned_path + ".gz")
        self.assertTrue(gzip_path.exists(), "Gzipped GeoJSON cache file should be created")

    def test_cached_json_content_valid(self):
        returned = regenerate_geojson_cache()
        with open(returned, "r") as f:
            obj = json.load(f)
        self.assertEqual(obj.get("type"), "FeatureCollection")
        self.assertIn("features", obj)
        self.assertEqual(len(obj["features"]), Work.objects.filter(status="p").count())

    def test_cached_gzip_can_be_unpacked(self):
        returned = regenerate_geojson_cache()
        gzip_path = returned + ".gz"
        with gzip.open(gzip_path, "rt") as f:
            obj = json.load(f)
        self.assertEqual(obj.get("type"), "FeatureCollection")
        self.assertIn("features", obj)
        self.assertEqual(len(obj["features"]), Work.objects.filter(status="p").count())

    def test_download_geopackage(self):
        url = reverse("optimap:download_geopackage")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/geopackage+sqlite3")
        self.assertRegex(response["Content-Disposition"], r"optimap_data_dump_.*\.gpkg")

    def test_data_page_hides_and_shows_links_correctly(self):
        cache_dir = Path(tempfile.gettempdir()) / "optimap_cache"
        for f in cache_dir.glob("optimap_data_dump_*"):
            f.unlink()

        response = self.client.get(reverse("optimap:data"))
        content = response.content.decode()
        self.assertNotIn("Download GeoJSON", content)
        self.assertNotIn("Download GeoPackage", content)
        self.assertNotIn("Download CSV", content)

        regenerate_geojson_cache()
        response = self.client.get(reverse("optimap:data"))
        content = response.content.decode()
        self.assertIn("Download GeoJSON", content)
        self.assertNotIn("Download GeoPackage", content)
        self.assertNotIn("Download CSV", content)

        regenerate_geopackage_cache()
        response = self.client.get(reverse("optimap:data"))
        content = response.content.decode()
        self.assertIn("Download GeoJSON", content)
        self.assertIn("Download GeoPackage", content)
        self.assertNotIn("Download CSV", content)

        regenerate_csv_cache()
        response = self.client.get(reverse("optimap:data"))
        content = response.content.decode()
        self.assertIn("Download GeoJSON", content)
        self.assertIn("Download GeoPackage", content)
        self.assertIn("Download CSV", content)

    def test_regenerate_csv_cache_creates_file(self):
        cache_dir = Path(tempfile.gettempdir()) / "optimap_cache"
        for f in cache_dir.glob("optimap_data_dump_*"):
            f.unlink()

        returned_path = regenerate_csv_cache()
        self.assertIsNotNone(returned_path, "ogr2ogr CSV conversion should succeed")
        self.assertTrue(Path(returned_path).exists(), "CSV cache file should be created")
        self.assertTrue(returned_path.endswith(".csv"))

    def test_csv_content_valid(self):
        # Publish one work so the CSV has at least one data row to inspect.
        pub = Work.objects.first()
        pub.status = "p"
        pub.save()

        csv_path = regenerate_csv_cache()
        self.assertIsNotNone(csv_path)
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), Work.objects.filter(status="p").count())
        self.assertIn("WKT", rows[0], "CSV must carry a WKT geometry column (issue #206)")
        valid_prefixes = (
            "POINT",
            "MULTIPOINT",
            "LINESTRING",
            "MULTILINESTRING",
            "POLYGON",
            "MULTIPOLYGON",
            "GEOMETRYCOLLECTION",
        )
        self.assertTrue(
            rows[0]["WKT"].upper().startswith(valid_prefixes),
            f"WKT column should hold a valid geometry type, got: {rows[0]['WKT']!r}",
        )
        self.assertIn("title", rows[0], "CSV should expose the work title alongside the geometry")

    def test_download_csv_endpoint(self):
        regenerate_csv_cache()
        url = reverse("optimap:download_csv")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertRegex(response["Content-Disposition"], r"optimap_data_dump_.*\.csv")

    def test_regenerate_all_data_dumps_creates_all_three(self):
        cache_dir = Path(tempfile.gettempdir()) / "optimap_cache"
        for f in cache_dir.glob("optimap_data_dump_*"):
            f.unlink()

        result = regenerate_all_data_dumps()
        self.assertSetEqual(set(result.keys()), {"geojson", "gpkg", "csv"})
        for fmt, path in result.items():
            self.assertIsNotNone(path, f"{fmt} dump should be produced")
            self.assertTrue(Path(path).exists(), f"{fmt} dump file should exist on disk")

    def test_geopackage_list_fields_no_stringlist_warning(self):
        # GeoJSON array fields become OGR StringList, which GPKG has no native
        # type for. We pin the conversion to String(JSON) so ogr2ogr no longer
        # warns on every run -- assert that benign warning is gone while the
        # values still round-trip as clean JSON arrays (not the lossy "(n:..)"
        # form a plain String cast would produce).
        pub = Work.objects.first()
        pub.status = "p"
        pub.authors = ["Doe, Jane", "Roe, Max"]
        pub.keywords = ["geo", "maps"]
        pub.save()

        with self.assertLogs("works.tasks", level="INFO") as cm:
            gpkg_path = convert_geojson_to_geopackage(regenerate_geojson_cache())
        self.assertIsNotNone(gpkg_path, "ogr2ogr GPKG conversion should succeed")
        self.assertNotIn(
            "does not seem to natively support StringList",
            "\n".join(cm.output),
            "GPKG conversion should not emit the StringList downgrade warning",
        )

        # ogr2ogr names the layer after the source filename, so open the default layer.
        with fiona.open(gpkg_path) as layer:
            authors = next(feat["properties"]["authors"] for feat in layer if feat["properties"].get("authors"))
        # The JSON subtype lets readers parse the column; fiona returns it as a
        # list. Accept a raw JSON string too in case a reader leaves it as text.
        if isinstance(authors, str):
            authors = json.loads(authors)
        self.assertEqual(
            authors,
            ["Doe, Jane", "Roe, Max"],
            "GPKG list columns should round-trip as JSON arrays, not the lossy '(n:..)' form",
        )

    def test_dump_has_source_and_collections_fields(self):
        """GeoJSON dump replaces FK integer source with source_name + source_url + collections."""
        coll = Collection.objects.create(identifier="test-coll", name="Test Collection")
        src = Source.objects.create(name="Dump Source", url_field="http://example.com/src")
        work = Work.objects.create(
            title="Dump Test Work",
            doi="10.0001/dump",
            url="http://example.com/dump",
            geometry="GEOMETRYCOLLECTION(POINT(7.0 51.0))",
            source=src,
            status="p",
        )
        work.collections.add(coll)

        path = regenerate_geojson_cache()
        with open(path) as f:
            data = json.load(f)

        features = data["features"]
        self.assertEqual(len(features), 1)
        props = features[0]["properties"]

        # Meaningful source fields present
        self.assertEqual(props["source_name"], "Dump Source")
        self.assertRegex(props["source_url"], rf"/api/v1/sources/{src.pk}/$")
        self.assertEqual(props["collections"], ["test-coll"])

        # Raw FK integers and internal fields must be absent
        for absent in ("source", "job", "created_by", "updated_by", "status", "provenance"):
            self.assertNotIn(absent, props, f"'{absent}' should not appear in the dump")

    def test_dump_skips_published_works_without_geometry(self):
        """A published work with a NULL/empty geometry must not crash the dump.

        Regression: ``regenerate_geojson_cache`` used to select every published
        work regardless of geometry, so a single geometry-less work raised
        ``AttributeError: 'NoneType' object has no attribute 'geojson'`` and took
        down the whole scheduled ``regenerate_all_data_dumps`` run.
        """
        src = Source.objects.create(name="Geo Source", url_field="http://example.com/geo")
        Work.objects.create(
            title="Has Geometry",
            doi="10.0001/hasgeom",
            url="http://example.com/hasgeom",
            geometry="GEOMETRYCOLLECTION(POINT(7.0 51.0))",
            source=src,
            status="p",
        )
        Work.objects.create(
            title="No Geometry",
            doi="10.0001/nogeom",
            url="http://example.com/nogeom",
            geometry=None,
            source=src,
            status="p",
        )

        path = regenerate_geojson_cache()
        with open(path) as f:
            data = json.load(f)

        titles = {feat["properties"]["title"] for feat in data["features"]}
        self.assertEqual(titles, {"Has Geometry"})
