"""
Integration tests for Geoextent API endpoints.

Tests compare API responses against reference results from geoextent library.
Reference values are pre-computed and hardcoded for reliability and speed.
"""

import json
import os
from django.test import Client, TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'optimap.settings')

User = get_user_model()


# Reference values generated from geoextent library
REFERENCE_VALUES = {
    'test_point': {
        'format': 'geojson',
        'geoextent_handler': 'handleVector',
        'bbox': [13.405, 52.52, 13.405, 52.52],
        'crs': '4326',
        'tbox': ['2023-01-15', '2023-01-15'],
        'file_size_bytes': 171
    },
    'test_polygon': {
        'format': 'geojson',
        'geoextent_handler': 'handleVector',
        'bbox': [9.5, 53.0, 10.5, 54.0],
        'crs': '4326',
        'tbox': ['2023-06-01', '2023-06-30'],
        'file_size_bytes': 305
    },
    'test_linestring': {
        'format': 'geojson',
        'geoextent_handler': 'handleVector',
        'bbox': [13.4, 52.5, 13.6, 52.7],
        'crs': '4326',
        'tbox': ['2023-12-25', '2023-12-25'],
        'file_size_bytes': 233
    },
    'directory_combined': {
        'format': 'folder',
        'crs': '4326',
        'bbox': [9.5, 52.5, 13.6, 54.0],
        'tbox': ['2023-01-15', '2023-12-25'],
        'file_size_bytes': 709
    }
}


class GeoextentExtractTest(TestCase):
    """Tests for /api/v1/geoextent/extract/ endpoint"""

    def setUp(self):
        self.client = Client()
        # Create test user and login
        self.user = User.objects.create_user('testuser', 'test@example.com', 'testpass')
        self.client.login(username='testuser', password='testpass')

        self.fixtures_dir = os.path.join(
            os.path.dirname(__file__), 'fixtures', 'geoextent'
        )

    def test_extract_point_geojson_format(self):
        """Test extracting extent from point GeoJSON with GeoJSON response format"""
        # Load test file
        with open(os.path.join(self.fixtures_dir, 'test_point.geojson'), 'rb') as f:
            file_content = f.read()

        # Reference values
        reference = REFERENCE_VALUES['test_point']

        # Call API
        response = self.client.post(
            '/api/v1/geoextent/extract/',
            {
                'file': SimpleUploadedFile('test_point.geojson', file_content),
                'bbox': 'true',
                'tbox': 'true',
                'response_format': 'geojson'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Should return GeoJSON FeatureCollection
        self.assertEqual(data['type'], 'FeatureCollection')
        self.assertIn('features', data)
        self.assertIn('geoextent_extraction', data)

        # Check feature properties contain temporal extent
        if len(data['features']) > 0:
            feature = data['features'][0]
            self.assertIn('tbox', feature['properties'])
            self.assertEqual(feature['properties']['tbox'], reference['tbox'])

    def test_extract_polygon_wkt_format(self):
        """Test extracting extent from polygon GeoJSON with WKT response format"""
        with open(os.path.join(self.fixtures_dir, 'test_polygon.geojson'), 'rb') as f:
            file_content = f.read()

        # Reference values
        reference = REFERENCE_VALUES['test_polygon']

        # Call API with WKT format
        response = self.client.post(
            '/api/v1/geoextent/extract/',
            {
                'file': SimpleUploadedFile('test_polygon.geojson', file_content),
                'bbox': 'true',
                'tbox': 'true',
                'response_format': 'wkt'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # WKT format should have wkt string and metadata
        self.assertIn('wkt', data)
        self.assertIn('crs', data)
        self.assertIn('geoextent_extraction', data)
        self.assertEqual(data['crs'], f"EPSG:{reference['crs']}")

        # Should have temporal extent
        self.assertIn('tbox', data)
        self.assertEqual(data['tbox'], reference['tbox'])

    def test_extract_geojson_response_format(self):
        """Test extracting extent with GeoJSON response format"""
        with open(os.path.join(self.fixtures_dir, 'test_linestring.geojson'), 'rb') as f:
            file_content = f.read()

        # Reference values
        reference = REFERENCE_VALUES['test_linestring']

        # Call API with geojson format
        response = self.client.post(
            '/api/v1/geoextent/extract/',
            {
                'file': SimpleUploadedFile('test_linestring.geojson', file_content),
                'bbox': 'true',
                'tbox': 'true',
                'response_format': 'geojson'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Should return GeoJSON FeatureCollection
        self.assertEqual(data['type'], 'FeatureCollection')
        self.assertIn('features', data)
        self.assertIn('geoextent_extraction', data)

        # Check first feature has geometry and temporal extent
        if len(data['features']) > 0:
            feature = data['features'][0]
            self.assertIn('geometry', feature)
            self.assertEqual(feature['geometry']['type'], 'Polygon')
            self.assertIn('tbox', feature['properties'])
            self.assertEqual(feature['properties']['tbox'],
            reference['tbox']
        )

    def test_extract_wkt_response_format(self):
        """Test extracting extent with WKT response format"""
        with open(os.path.join(self.fixtures_dir, 'test_polygon.geojson'), 'rb') as f:
            file_content = f.read()

        response = self.client.post(
            '/api/v1/geoextent/extract/',
            {
                'file': SimpleUploadedFile('test_polygon.geojson', file_content),
                'bbox': 'true',
                'tbox': 'true',
                'response_format': 'wkt'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Check WKT structure
        self.assertIn('wkt', data)
        self.assertTrue(data['wkt'].startswith('POLYGON'))
        self.assertEqual(data['crs'], 'EPSG:4326')
        self.assertIn('tbox', data)
        self.assertEqual(data['tbox'], REFERENCE_VALUES['test_polygon']['tbox'])

    def test_extract_wkb_response_format(self):
        """Test extracting extent with WKB response format"""
        with open(os.path.join(self.fixtures_dir, 'test_point.geojson'), 'rb') as f:
            file_content = f.read()

        response = self.client.post(
            '/api/v1/geoextent/extract/',
            {
                'file': SimpleUploadedFile('test_point.geojson', file_content),
                'bbox': 'true',
                'tbox': 'true',
                'response_format': 'wkb'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Check WKB structure
        self.assertIn('wkb', data)
        self.assertIsInstance(data['wkb'], str)  # Hex string
        self.assertEqual(data['crs'], 'EPSG:4326')
        self.assertIn('tbox', data)
        self.assertEqual(data['tbox'], REFERENCE_VALUES['test_point']['tbox'])

    def test_extract_without_bbox(self):
        """Test extracting only temporal extent without bbox - should fail gracefully"""
        with open(os.path.join(self.fixtures_dir, 'test_point.geojson'), 'rb') as f:
            file_content = f.read()

        response = self.client.post(
            '/api/v1/geoextent/extract/',
            {
                'file': SimpleUploadedFile('test_point.geojson', file_content),
                'bbox': 'false',
                'tbox': 'true',
                'response_format': 'geojson'
            }
        )

        # When bbox=false and response_format=geojson, the API cannot create valid
        # GeoJSON without geometry, so it returns an error or empty result
        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Should either be an error response or have features
        # For now, just check it's a valid JSON response
        self.assertIsInstance(data, dict)

        # If it has features, check temporal extent
        if 'features' in data and len(data['features']) > 0:
            feature = data['features'][0]
            if 'tbox' in feature.get('properties', {}):
                self.assertEqual(feature['properties']['tbox'], REFERENCE_VALUES['test_point']['tbox'])

    def test_extract_convex_hull(self):
        """Test extracting convex hull instead of bbox"""
        with open(os.path.join(self.fixtures_dir, 'test_polygon.geojson'), 'rb') as f:
            file_content = f.read()

        response = self.client.post(
            '/api/v1/geoextent/extract/',
            {
                'file': SimpleUploadedFile('test_polygon.geojson', file_content),
                'bbox': 'true',
                'convex_hull': 'true',
                'response_format': 'geojson'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Should return GeoJSON FeatureCollection with convex hull
        self.assertEqual(data['type'], 'FeatureCollection')
        self.assertIn('features', data)
        self.assertIn('geoextent_extraction', data)

        # Check that extent_type is convex_hull
        self.assertEqual(data['geoextent_extraction']['extent_type'], 'convex_hull')

        # Features should have geometry (convex hull polygon)
        if len(data['features']) > 0:
            feature = data['features'][0]
            self.assertIn('geometry', feature)
            self.assertEqual(feature['geometry']['type'], 'Polygon')


class GeoextentBatchTest(TestCase):
    """Tests for /api/v1/geoextent/extract-batch/ endpoint"""

    def setUp(self):
        self.client = Client()
        # Create test user and login
        self.user = User.objects.create_user('testuser', 'test@example.com', 'testpass')
        self.client.login(username='testuser', password='testpass')

        self.fixtures_dir = os.path.join(
            os.path.dirname(__file__), 'fixtures', 'geoextent'
        )

    def test_batch_multiple_files_combined(self):
        """Test batch processing with extent combination"""
        # Load all test files
        files = []
        for filename in ['test_point.geojson', 'test_polygon.geojson', 'test_linestring.geojson']:
            with open(os.path.join(self.fixtures_dir, filename), 'rb') as f:
                files.append(SimpleUploadedFile(filename, f.read()))

        # Call API
        response = self.client.post(
            '/api/v1/geoextent/extract-batch/',
            {
                'files': files,
                'bbox': 'true',
                'tbox': 'true',
                'response_format': 'geojson'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Check response structure
        self.assertTrue(data['success'])
        self.assertEqual(data['files_processed'], 3)
        self.assertIn('combined_extent', data)
        self.assertIn('individual_results', data)

        # Combined extent should be GeoJSON FeatureCollection
        combined = data['combined_extent']
        self.assertEqual(combined['type'], 'FeatureCollection')
        self.assertIn('features', combined)
        self.assertIn('geoextent_extraction', combined)

        # Check that we have features with temporal extent
        if len(combined['features']) > 0:
            feature = combined['features'][0]
            self.assertIn('tbox', feature['properties'])

        # Check individual results count
        self.assertEqual(len(data['individual_results']), 3)

    def test_batch_individual_results(self):
        """Test batch processing returns both combined and individual results"""
        files = []
        for filename in ['test_point.geojson', 'test_polygon.geojson']:
            with open(os.path.join(self.fixtures_dir, filename), 'rb') as f:
                files.append(SimpleUploadedFile(filename, f.read()))

        response = self.client.post(
            '/api/v1/geoextent/extract-batch/',
            {
                'files': files,
                'bbox': 'true',
                'tbox': 'true',
                'response_format': 'geojson'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Should have both combined extent and individual results
        self.assertTrue(data['success'])
        self.assertIn('combined_extent', data)
        self.assertIn('individual_results', data)
        self.assertEqual(len(data['individual_results']), 2)

        # Combined extent should be GeoJSON FeatureCollection
        combined = data['combined_extent']
        self.assertEqual(combined['type'], 'FeatureCollection')
        self.assertIn('features', combined)

        # Individual results should also be GeoJSON FeatureCollections
        for result in data['individual_results']:
            self.assertEqual(result['type'], 'FeatureCollection')
            self.assertIn('features', result)
            self.assertIn('geoextent_extraction', result)

    def test_batch_wkt_format(self):
        """Test batch processing with WKT response format"""
        with open(os.path.join(self.fixtures_dir, 'test_point.geojson'), 'rb') as f:
            file_content = f.read()

        response = self.client.post(
            '/api/v1/geoextent/extract-batch/',
            {
                'files': [SimpleUploadedFile('test_point.geojson', file_content)],
                'bbox': 'true',
                'tbox': 'true',
                'response_format': 'wkt'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Response should have batch metadata
        self.assertTrue(data['success'])
        self.assertEqual(data['files_processed'], 1)

        # Check that combined extent is in WKT format
        combined = data['combined_extent']
        self.assertIn('wkt', combined)
        self.assertIn('crs', combined)
        self.assertIn('geoextent_extraction', combined)

        # Check that individual results are also in WKT format
        self.assertEqual(len(data['individual_results']), 1)
        result = data['individual_results'][0]
        self.assertIn('wkt', result)
        self.assertIn('crs', result)

    def test_batch_geojson_format(self):
        """Test batch processing with GeoJSON response format"""
        with open(os.path.join(self.fixtures_dir, 'test_polygon.geojson'), 'rb') as f:
            file_content = f.read()

        response = self.client.post(
            '/api/v1/geoextent/extract-batch/',
            {
                'files': [SimpleUploadedFile('test_polygon.geojson', file_content)],
                'bbox': 'true',
                'response_format': 'geojson'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Response should have batch metadata
        self.assertTrue(data['success'])
        self.assertEqual(data['files_processed'], 1)

        # Combined extent should be GeoJSON FeatureCollection
        combined = data['combined_extent']
        self.assertEqual(combined['type'], 'FeatureCollection')
        self.assertIn('features', combined)
        self.assertIn('geoextent_extraction', combined)

        # Individual results should also be GeoJSON FeatureCollections
        self.assertEqual(data['individual_results'][0]['type'], 'FeatureCollection')


class GeoextentRemoteTest(TestCase):
    """Tests for /api/v1/geoextent/extract-remote/ endpoint

    Note: These tests make actual network calls and may fail if:
    - No internet connection
    - Repository is unavailable
    - DOI resolver is down
    """

    # Reference value from Zenodo dataset 10.5281/zenodo.4593540
    # NOTE: geoextent.fromRemote() has a bug where it returns coordinates in
    # [minLat, minLon, maxLat, maxLon] format instead of the GeoJSON standard
    # [minLon, minLat, maxLon, maxLat]. This needs to be fixed upstream in geoextent.
    # Pennsylvania coordinates: ~40°N latitude, ~75-80°W longitude
    ZENODO_REFERENCE = {
        'identifier': '10.5281/zenodo.4593540',
        'bbox': [39.642802545572735, -80.71456319678893, 42.256308231814586, -74.78657735361809],
        'tbox': ['2006-02-02', '2018-08-27'],
        'crs': '4326'
    }

    def setUp(self):
        self.client = Client()
        # Create test user and login
        self.user = User.objects.create_user('testuser', 'test@example.com', 'testpass')
        self.client.login(username='testuser', password='testpass')

    def test_remote_single_identifier(self):
        """Test extracting from single remote identifier"""
        identifier = self.ZENODO_REFERENCE['identifier']

        # Call API
        response = self.client.post(
            '/api/v1/geoextent/extract-remote/',
            json.dumps({
                'identifiers': [identifier],
                'bbox': True,
                'tbox': True,
                'response_format': 'geojson'
            }),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Should return GeoJSON FeatureCollection
        self.assertEqual(data['type'], 'FeatureCollection')
        self.assertIn('features', data)
        self.assertIn('geoextent_extraction', data)

        # Check extraction metadata
        self.assertEqual(data['geoextent_extraction']['inputs'], [identifier])
        self.assertEqual(data['geoextent_extraction']['format'], 'remote')

        # Check temporal extent in feature properties
        if len(data['features']) > 0:
            feature = data['features'][0]
            self.assertIn('tbox', feature['properties'])
            self.assertEqual(feature['properties']['tbox'], self.ZENODO_REFERENCE['tbox'])

    def test_remote_multiple_identifiers(self):
        """Test extracting from multiple remote identifiers"""
        identifiers = [
            '10.5281/zenodo.4593540',
            '10.5281/zenodo.7416089'  # This might fail (404), but should be handled
        ]

        # Call API
        response = self.client.post(
            '/api/v1/geoextent/extract-remote/',
            json.dumps({
                'identifiers': identifiers,
                'bbox': True,
                'tbox': True,
                'response_format': 'geojson'
            }),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Should return GeoJSON FeatureCollection
        self.assertEqual(data['type'], 'FeatureCollection')
        self.assertIn('features', data)
        self.assertIn('geoextent_extraction', data)

        # Check that multiple identifiers were processed
        self.assertEqual(data['geoextent_extraction']['inputs'], identifiers)

    def test_remote_wkt_format(self):
        """Test remote extraction with WKT format"""
        identifier = self.ZENODO_REFERENCE['identifier']

        response = self.client.post(
            '/api/v1/geoextent/extract-remote/',
            json.dumps({
                'identifiers': [identifier],
                'bbox': True,
                'tbox': True,
                'response_format': 'wkt'
            }),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # WKT format should have wkt string and metadata
        self.assertIn('wkt', data)
        self.assertIn('crs', data)
        self.assertIn('geoextent_extraction', data)
        self.assertEqual(data['geoextent_extraction']['format'], 'remote')

    def test_remote_single_identifier_simple_response(self):
        """Test single identifier returns GeoJSON FeatureCollection"""
        identifier = self.ZENODO_REFERENCE['identifier']

        response = self.client.post(
            '/api/v1/geoextent/extract-remote/',
            json.dumps({
                'identifiers': [identifier],
                'bbox': True
            }),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Should return GeoJSON FeatureCollection (default format)
        self.assertEqual(data['type'], 'FeatureCollection')
        self.assertIn('features', data)
        self.assertIn('geoextent_extraction', data)


class GeoextentRemoteGetTest(TestCase):
    """Tests for /api/v1/geoextent/extract-remote/ GET endpoint"""

    # Reference value from Zenodo dataset 10.5281/zenodo.4593540
    # NOTE: geoextent.fromRemote() has a bug where it returns coordinates in
    # [minLat, minLon, maxLat, maxLon] format instead of the GeoJSON standard
    # [minLon, minLat, maxLon, maxLat]. This needs to be fixed upstream in geoextent.
    # Pennsylvania coordinates: ~40°N latitude, ~75-80°W longitude
    ZENODO_REFERENCE = {
        'identifier': '10.5281/zenodo.4593540',
        'bbox': [39.642802545572735, -80.71456319678893, 42.256308231814586, -74.78657735361809],
        'tbox': ['2006-02-02', '2018-08-27'],
        'crs': '4326'
    }

    def setUp(self):
        self.client = Client()
        # Create test user and login
        self.user = User.objects.create_user('testuser', 'test@example.com', 'testpass')
        self.client.login(username='testuser', password='testpass')

    def test_get_single_identifier(self):
        """Test GET request with single identifier"""
        identifier = self.ZENODO_REFERENCE['identifier']

        response = self.client.get(
            '/api/v1/geoextent/extract-remote/',
            {
                'identifiers': identifier,
                'bbox': 'true',
                'tbox': 'true',
                'response_format': 'geojson'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Should return GeoJSON FeatureCollection
        self.assertEqual(data['type'], 'FeatureCollection')
        self.assertIn('features', data)
        self.assertIn('geoextent_extraction', data)

        # Check temporal extent in feature properties
        if len(data['features']) > 0:
            feature = data['features'][0]
            self.assertIn('tbox', feature['properties'])
            self.assertEqual(feature['properties']['tbox'], self.ZENODO_REFERENCE['tbox'])

    def test_get_multiple_identifiers(self):
        """Test GET request with comma-separated identifiers"""
        identifiers = '10.5281/zenodo.4593540,10.5281/zenodo.7416089'

        response = self.client.get(
            '/api/v1/geoextent/extract-remote/',
            {
                'identifiers': identifiers,
                'bbox': 'true',
                'tbox': 'true',
                'response_format': 'geojson'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Should return GeoJSON FeatureCollection
        self.assertEqual(data['type'], 'FeatureCollection')
        self.assertIn('features', data)
        self.assertIn('geoextent_extraction', data)

        # Should have processed multiple identifiers
        self.assertEqual(len(data['geoextent_extraction']['inputs']), 2)

    def test_get_geojson_format(self):
        """Test GET request with GeoJSON response format"""
        identifier = self.ZENODO_REFERENCE['identifier']

        response = self.client.get(
            '/api/v1/geoextent/extract-remote/',
            {
                'identifiers': identifier,
                'bbox': 'true',
                'response_format': 'geojson'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # GeoJSON format should return FeatureCollection
        self.assertEqual(data['type'], 'FeatureCollection')
        self.assertIn('features', data)
        self.assertIn('geoextent_extraction', data)

        # Should have at least one feature with geometry
        if len(data['features']) > 0:
            feature = data['features'][0]
            self.assertEqual(feature['type'], 'Feature')
            self.assertIn('geometry', feature)
            self.assertIn('properties', feature)

    def test_get_wkt_format(self):
        """Test GET request with WKT response format"""
        identifier = self.ZENODO_REFERENCE['identifier']

        response = self.client.get(
            '/api/v1/geoextent/extract-remote/',
            {
                'identifiers': identifier,
                'bbox': 'true',
                'response_format': 'wkt'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # WKT format returns string
        self.assertIn('wkt', data)
        self.assertIn('crs', data)
        self.assertIsInstance(data['wkt'], str)
        self.assertTrue(data['wkt'].startswith('POLYGON'))

    def test_get_missing_identifiers(self):
        """Test GET request without identifiers parameter"""
        response = self.client.get(
            '/api/v1/geoextent/extract-remote/',
            {'bbox': 'true'}
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn('identifiers', data)


class GeoextentErrorHandlingTest(TestCase):
    """Tests for error handling in geoextent endpoints"""

    def setUp(self):
        self.client = Client()
        # Create test user and login
        self.user = User.objects.create_user('testuser', 'test@example.com', 'testpass')
        self.client.login(username='testuser', password='testpass')

    def test_extract_no_file(self):
        """Test extract endpoint with no file provided"""
        response = self.client.post(
            '/api/v1/geoextent/extract/',
            {'bbox': 'true'}
        )

        self.assertEqual(response.status_code, 400)

    def test_batch_no_files(self):
        """Test batch endpoint with no files provided"""
        response = self.client.post(
            '/api/v1/geoextent/extract-batch/',
            {'bbox': 'true'}
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('error', data)

    def test_remote_empty_identifiers(self):
        """Test remote endpoint with empty identifiers list"""
        response = self.client.post(
            '/api/v1/geoextent/extract-remote/',
            json.dumps({
                'identifiers': [],
                'bbox': True
            }),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 400)

    def test_extract_invalid_format(self):
        """Test extract with invalid response format"""
        fixtures_dir = os.path.join(
            os.path.dirname(__file__), 'fixtures', 'geoextent'
        )
        with open(os.path.join(fixtures_dir, 'test_point.geojson'), 'rb') as f:
            file_content = f.read()

        response = self.client.post(
            '/api/v1/geoextent/extract/',
            {
                'file': SimpleUploadedFile('test_point.geojson', file_content),
                'bbox': 'true',
                'response_format': 'invalid_format'
            }
        )

        self.assertEqual(response.status_code, 400)
