import os
import json
import tempfile
import gzip
from datetime import datetime, timedelta
from django.test import TestCase
from django.core.serializers import serialize
import fiona
from django.urls import reverse
from django.conf import settings
import re
from publications.models import Publication, Source
from publications.views import generate_geopackage
from publications.tasks import (
    regenerate_geojson_cache,
    regenerate_geopackage_cache,
    generate_data_dump_filename,
    cleanup_old_data_dumps,
)
from pathlib import Path

class GeoDataAlternativeTestCase(TestCase):
    def setUp(self):
        Publication.objects.all().delete()
        wkt_point1 = "GEOMETRYCOLLECTION(POINT(7.59573 51.96944))"
        wkt_point2 = "GEOMETRYCOLLECTION(POINT(8.59573 52.96944))"
        wkt_point3 = "GEOMETRYCOLLECTION(POINT(9.59573 53.96944))"
        
        s1 = Source.objects.create(name="Source One", url_field="http://example.com/1")
        Publication.objects.create(
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
        Publication.objects.create(
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
        Publication.objects.create(
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
        geojson_data = serialize('geojson', Publication.objects.all(), geometry_field='geometry')
        self.assertTrue(len(geojson_data) > 0, "GeoJSON data should not be empty")
        geojson_obj = json.loads(geojson_data)
        self.assertEqual(geojson_obj.get("type"), "FeatureCollection", "GeoJSON type should be FeatureCollection")
        features = geojson_obj.get("features", [])
        self.assertEqual(len(features), Publication.objects.count(), "Feature count should match Publication count")
        self.assertIn("title", features[0]["properties"], "Each feature should have a 'title' property")

    def test_geopackage_generation(self):
        gpkg_path = generate_geopackage()
        self.assertTrue(os.path.exists(gpkg_path), "GeoPackage file should exist")
        with fiona.open(gpkg_path, layer='publications') as layer:
            features = list(layer)
            self.assertEqual(len(features), Publication.objects.count(),
                             "Feature count in GeoPackage should match the Publication count")

    def test_update_reflects_in_generated_data(self):
        initial_geojson = serialize('geojson', Publication.objects.all(), geometry_field='geometry')
        initial_obj = json.loads(initial_geojson)
        initial_title = initial_obj['features'][0]['properties']['title']
        pub = Publication.objects.first()
        pub.title += " Updated"
        pub.save()
        
        updated_geojson = serialize('geojson', Publication.objects.all(), geometry_field='geometry')
        updated_obj = json.loads(updated_geojson)
        updated_title = updated_obj['features'][0]['properties']['title']
        self.assertNotEqual(initial_title, updated_title,
                            "The title of the first publication should update in the GeoJSON output")

        # Test GeoPackage update
        initial_gpkg = open(generate_geopackage(), 'rb').read()
        updated_gpkg = open(generate_geopackage(), 'rb').read()
        self.assertNotEqual(initial_gpkg, updated_gpkg,
                            "The GeoPackage data should update when a Publication changes")

    def test_data_endpoint(self):
        url = reverse('optimap:data')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, "Data endpoint should return status 200")
        content = response.content.decode()
        self.assertIn(f"Data dumps run every {settings.DATA_DUMP_INTERVAL_HOURS} hour", content)
        cache_dir = Path(tempfile.gettempdir()) / 'optimap_cache'
        dumps = sorted(cache_dir.glob('optimap_data_dump_*.geojson'), reverse=True)
        self.assertTrue(dumps, "At least one data dump should exist for Last updated check")
        self.assertIn("Last updated:", content)

    def test_download_geojson_gzip(self):
        regenerate_geojson_cache()
        url = reverse('optimap:download_geojson')
        response = self.client.get(url, HTTP_ACCEPT_ENCODING='gzip')
        self.assertEqual(response.status_code, 200, "Gzip download should return status 200")
        self.assertEqual(response['Content-Encoding'], 'gzip', "Response should be gzipped when requested")
        self.assertEqual(response['Content-Type'], 'application/json', "Content-Type should be application/json")
        self.assertRegex(response['Content-Disposition'], r'optimap_data_dump_.*\.geojson\.gz')

    def test_download_geopackage_endpoint(self):
        url = reverse('optimap:download_geopackage')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, "GeoPackage endpoint should return 200")
        self.assertEqual(response['Content-Type'], 'application/geopackage+sqlite3',
                         "Content-Type should be application/geopackage+sqlite3")
        self.assertRegex(response['Content-Disposition'], r'optimap_data_dump_.*\.gpkg')

    def test_regenerate_geojson_cache_creates_files(self):
        cache_dir = Path(tempfile.gettempdir()) / 'optimap_cache'
        for f in cache_dir.glob('optimap_data_dump_*'):
            f.unlink()

        returned_path = regenerate_geojson_cache()
        self.assertTrue(Path(returned_path).exists(), "GeoJSON cache file should be created")
        self.assertTrue(returned_path.endswith('.geojson'))

        gzip_path = Path(returned_path + '.gz')
        self.assertTrue(gzip_path.exists(), "Gzipped GeoJSON cache file should be created")

    def test_cached_json_content_valid(self):
        returned = regenerate_geojson_cache()
        with open(returned, 'r') as f:
            obj = json.load(f)
        self.assertEqual(obj.get('type'), 'FeatureCollection')
        self.assertIn('features', obj)
        self.assertEqual(len(obj['features']), Publication.objects.filter(status='p').count())

    def test_cached_gzip_can_be_unpacked(self):
        returned = regenerate_geojson_cache()
        gzip_path = returned + '.gz'
        with gzip.open(gzip_path, 'rt') as f:
            obj = json.load(f)
        self.assertEqual(obj.get('type'), 'FeatureCollection')
        self.assertIn('features', obj)
        self.assertEqual(len(obj['features']), Publication.objects.filter(status='p').count())

    def test_download_geopackage(self):
        url = reverse('optimap:download_geopackage')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/geopackage+sqlite3')
        self.assertRegex(response['Content-Disposition'], r'optimap_data_dump_.*\.gpkg')

    def test_data_page_hides_and_shows_links_correctly(self):
        cache_dir = Path(tempfile.gettempdir()) / 'optimap_cache'
        for f in cache_dir.glob('optimap_data_dump_*'):
            f.unlink()

        response = self.client.get(reverse('optimap:data'))
        content = response.content.decode()
        self.assertNotIn('Download GeoJSON', content)
        self.assertNotIn('Download GeoPackage', content)

        regenerate_geojson_cache()
        response = self.client.get(reverse('optimap:data'))
        content = response.content.decode()
        self.assertIn('Download GeoJSON', content)
        self.assertNotIn('Download GeoPackage', content)

        regenerate_geopackage_cache()
        response = self.client.get(reverse('optimap:data'))
        content = response.content.decode()
        self.assertIn('Download GeoJSON', content)
        self.assertIn('Download GeoPackage', content)
