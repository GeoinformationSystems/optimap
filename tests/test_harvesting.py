import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'optimap.settings')
django.setup()

from django.test import Client, TestCase
from publications.models import Publication, Source, HarvestingEvent, Schedule
import responses
import time
from django.contrib.auth import get_user_model

User = get_user_model()


class SimpleTest(TestCase):

    @classmethod
    @responses.activate
    def setUpClass(cls):
        super().setUpClass()
        Publication.objects.all().delete()

        article01_path = os.path.join(os.getcwd(), 'tests', 'harvesting', 'journal_1', 'article_01.html')
        article02_path = os.path.join(os.getcwd(), 'tests', 'harvesting', 'journal_1', 'article_02.html')
        with open(article01_path) as f1, open(article02_path) as f2:
            responses.get(
                'http://localhost:8330/index.php/opti-geo/article/view/1',
                body=f1.read()
            )
            responses.get(
                'http://localhost:8330/index.php/opti-geo/article/view/2',
                body=f2.read()
            )

        src = Source.objects.create(
            url_field="http://example.org/oai",
            harvest_interval_minutes=60
        )
        event = HarvestingEvent.objects.create(source=src, status="in_progress")

        oai_path = os.path.join(os.getcwd(), 'tests', 'harvesting', 'journal_1', 'oai_dc.xml')
        with open(oai_path, 'rb') as oai_file:
            xml_bytes = oai_file.read()

        from publications.tasks import parse_oai_xml_and_save_publications
        parse_oai_xml_and_save_publications(xml_bytes, event)

        Publication.objects.all().update(status="p")

        cls.user = User.objects.create_user(
            username="testuser",
            email="testuser@example.com",
            password="password123"
        )

    @classmethod
    def tearDownClass(cls):
        Publication.objects.all().delete()
        super().tearDownClass()

    def setUp(self):
        self.client = Client()
        results = self.client.get('/api/v1/publications/').json()['results']
        features = results.get('features', [])
        if len(features) >= 2:
            self.id1, self.id2 = features[1]['id'], features[0]['id']
        elif len(features) == 1:
            self.id1 = self.id2 = features[0]['id']
        else:
            self.id1 = self.id2 = None

    def test_api_root(self):
        response = self.client.get('/api/v1/publications/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get('Content-Type'), 'application/json')
        results = response.json()['results']
        self.assertEqual(results['type'], 'FeatureCollection')
        self.assertEqual(len(results['features']), 2)

    def test_api_publication_1(self):
        response = self.client.get(f'/api/v1/publications/{self.id1}.json')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['type'], 'Feature')
        self.assertEqual(body['geometry']['type'], 'GeometryCollection')
        self.assertEqual(body['geometry']['geometries'][0]['type'], 'LineString')
        self.assertEqual(body['properties']['title'], 'Test 1: One')
        self.assertEqual(body['properties']['publicationDate'], '2022-07-01')
        self.assertEqual(body['properties']['timeperiod_startdate'], ['2022-06-01'])
        self.assertEqual(
            body['properties']['url'],
            'http://localhost:8330/index.php/opti-geo/article/view/1'
        )

    def test_api_publication_2(self):
        response = self.client.get(f'/api/v1/publications/{self.id2}.json')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['type'], 'Feature')
        self.assertEqual(body['geometry']['type'], 'GeometryCollection')
        self.assertEqual(body['geometry']['geometries'][0]['type'], 'Polygon')
        self.assertEqual(body['properties']['title'], 'Test 2: Two')
        self.assertIsNone(body['properties']['doi'])
        self.assertEqual(body['properties']['timeperiod_enddate'], ['2022-03-31'])
        self.assertEqual(
            body['properties']['url'],
            'http://localhost:8330/index.php/opti-geo/article/view/2'
        )

    def test_task_scheduling(self):
        # ensure the scheduling action still works
        oai_file_path = os.path.join(os.getcwd(), "tests", "harvesting", "journal_1", "oai_dc.xml")
        new_src = Source.objects.create(
            url_field=f"file://{oai_file_path}",
            harvest_interval_minutes=60
        )
        time.sleep(2)
        schedule = Schedule.objects.filter(name=f"Harvest Source {new_src.id}")
        self.assertTrue(schedule.exists(), "Django-Q task not scheduled for source.")

    def test_no_duplicates(self):
        publications = Publication.objects.all()
        self.assertEqual(publications.count(), 2, "Expected exactly 2 unique publications")
        titles = [p.title for p in publications]
        self.assertEqual(len(titles), len(set(titles)), "Duplicate titles found")
