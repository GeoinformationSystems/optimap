import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'optimap.settings')
django.setup()
from django.test import Client, TestCase
from publications.tasks import parse_oai_xml_and_save_publications
from publications.models import Publication, Source, Schedule
from django_q.tasks import async_task
import httpretty
import time

class SimpleTest(TestCase):   
  
    def setUp(self):
        self.client = Client()

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(os.getcwd(), 'tests', 'harvesting', 'journal_1', 'oai_dc.xml')) as oai, open(os.path.join(os.getcwd(), 'tests', 'harvesting', 'journal_1', 'article_01.html')) as article01, open(os.path.join(os.getcwd(), 'tests', 'harvesting', 'journal_1', 'article_02.html')) as article02:
            httpretty.enable(verbose=True, allow_net_connect=False)  # enable HTTPretty so that it will monkey patch the socket module
            httpretty.register_uri(
                httpretty.GET,
                'http://localhost:8330/index.php/opti-geo/article/view/1',
                body = article01.read()
            )
            httpretty.register_uri(
                httpretty.GET,
                'http://localhost:8330/index.php/opti-geo/article/view/2',
                body = article02.read()
            )

            parse_oai_xml_and_save_publications(oai.read(), event=None)

    @classmethod
    def tearDownClass(cls):
        Publication.objects.all().delete()

    def test_api_root(self):
        response = self.client.get('/api/v1/publications/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get('Content-Type'), 'application/json')

        results = response.json()['results']

        self.assertEqual(results['type'], 'FeatureCollection')
        self.assertEqual(len(results['features']), 2)
        self.assertEqual(len(results['features'][0]['properties']), 9)
        self.assertEqual(results['features'][0]['properties']['title'], 'Test 1: One')
        self.assertEqual(results['features'][0]['properties']['publicationDate'], '2022-07-01')

    def test_api_publication_1(self):
        response = self.client.get('/api/v1/publications/1.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get('Content-Type'), 'application/json')

        body = response.json()
        self.assertEqual(body['type'], 'Feature')
        self.assertEqual(body['geometry']['type'], 'GeometryCollection')
        self.assertEqual(body['geometry']['geometries'][0]['type'], 'LineString')
        self.assertEqual(body['properties']['title'], 'Test 1: One')
        self.assertEqual(body['properties']['publicationDate'], '2022-07-01')
        self.assertEqual(body['properties']['timeperiod_startdate'],['2022-06-01'])
        self.assertEqual(body['properties']['url'],'http://localhost:8330/index.php/opti-geo/article/view/1')
        
    def test_api_publication_2(self):
        response = self.client.get('/api/v1/publications/2.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get('Content-Type'), 'application/json')

        body = response.json()
        self.assertEqual(body['type'], 'Feature')
        self.assertEqual(body['geometry']['type'], 'GeometryCollection')
        self.assertEqual(body['geometry']['geometries'][0]['type'], 'Polygon')
        self.assertIsNone(body['properties']['doi'])
        self.assertEqual(body['properties']['timeperiod_enddate'],['2022-03-31'])
        self.assertEqual(body['properties']['url'],'http://localhost:8330/index.php/opti-geo/article/view/2')

    def test_task_scheduling(self):
        oai_file_path = os.path.join(os.getcwd(), "tests", "harvesting", "journal_1", "oai_dc.xml")
        source = Source.objects.create(
            url_field=f"file://{oai_file_path}",
            harvest_interval_minutes=60
        )
        source.save()
        time.sleep(2)
        schedule = Schedule.objects.filter(name=f"Harvest Source {source.id}")
        self.assertTrue(schedule.exists(), "❌ Django-Q task not scheduled for source.")

        publications_count = Publication.objects.count()
        async_task("publications.tasks.harvest_oai_endpoint", source.id)
        time.sleep(5) 

        self.assertGreater(publications_count, 0, " No publications were harvested.")

        with open(oai_file_path, "r") as oai:
            content = oai.read()
            parse_oai_xml_and_save_publications(content, event=None)
            parse_oai_xml_and_save_publications(content, event=None)

        final_count = Publication.objects.count()
        self.assertEqual(final_count, publications_count, " Duplicate publications were created!")


    def test_no_duplicates(self):
        Publication.objects.all().delete()
        oai_file_path = os.path.join(os.getcwd(), "tests", "harvesting", "journal_1", "oai_dc.xml")
        print(Publication.objects.count())

        with open(oai_file_path, "r") as oai:
            content = oai.read()

            parse_oai_xml_and_save_publications(content, event=None)
            parse_oai_xml_and_save_publications(content, event=None)
    
        publications_count = Publication.objects.count()
        self.assertEqual(publications_count, 2, "Duplicate publications were created!")
