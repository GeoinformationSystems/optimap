import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'optimap.settings')
django.setup()
from django.test import Client, TestCase
from publications.tasks import parse_oai_xml_and_save_publications
from publications.models import Publication, Source, Schedule
from django_q.tasks import async_task
import responses
import time

class SimpleTest(TestCase):   
  
    def setUp(self):
        self.client = Client()

        results = self.client.get('/api/v1/publications/').json()['results']
        features = results.get('features', [])

        if len(features) >= 2:
            self.id1 = features[1]['id']
            self.id2 = features[0]['id']
        elif len(features) == 1:
            self.id1 = self.id2 = features[0]['id']
        else:
            self.id1 = self.id2 = None  

    @classmethod
    @responses.activate
    def setUpClass(cls):
        Publication.objects.all().delete()

        with open(os.path.join(os.getcwd(), 'tests', 'harvesting', 'journal_1', 'oai_dc.xml')) as oai, open(os.path.join(os.getcwd(), 'tests', 'harvesting', 'journal_1', 'article_01.html')) as article01, open(os.path.join(os.getcwd(), 'tests', 'harvesting', 'journal_1', 'article_02.html')) as article02:
            responses.get('http://localhost:8330/index.php/opti-geo/article/view/1',
                          body = article01.read())
            responses.get('http://localhost:8330/index.php/opti-geo/article/view/2',
                          body = article02.read())

            parse_oai_xml_and_save_publications(oai.read(), event=None)

            # set status to published
            Publication.objects.all().update(status="p")

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

    def test_api_publication_1(self):
        response = self.client.get('/api/v1/publications/%s.json' % self.id1)
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
        response = self.client.get('/api/v1/publications/%s.json' % self.id2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get('Content-Type'), 'application/json')

        body = response.json()
        self.assertEqual(body['type'], 'Feature')
        self.assertEqual(body['geometry']['type'], 'GeometryCollection')
        self.assertEqual(body['geometry']['geometries'][0]['type'], 'Polygon')
        self.assertEqual(body['properties']['title'], 'Test 2: Two')
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
        self.assertTrue(schedule.exists(), "Django-Q task not scheduled for source.")

        from publications.tasks import harvest_oai_endpoint
        harvest_oai_endpoint(source.id)

        publications_count = Publication.objects.count()
        self.assertGreater(publications_count, 0, "No publications were harvested.")

        with open(oai_file_path, "r") as oai:
            content = oai.read()
            parse_oai_xml_and_save_publications(content, event=None)
            parse_oai_xml_and_save_publications(content, event=None)

        final_count = Publication.objects.count()
        self.assertEqual(final_count, publications_count, "Duplicate publications were created!")

        publications_with_doi = Publication.objects.exclude(doi__isnull=True)

        self.assertTrue(publications_with_doi.exists(), "No publication with DOI found.")
        for pub in publications_with_doi:
            self.assertTrue(pub.doi.startswith("10."), f"DOI '{pub.doi}' is not correctly formatted.")


    def test_no_duplicates(self):   
        publications_count = Publication.objects.count()
        self.assertEqual(publications_count, 2, "Duplicate publications were created!")

        response = self.client.get('/api/v1/publications/')
        results = response.json()['results']

        titles = [pub['properties']['title'] for pub in results['features']]
        unique_titles = list(set(titles))
        self.assertEqual(len(titles), len(unique_titles))
