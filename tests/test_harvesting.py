import os
import django
import time
import responses
from django.test import Client, TransactionTestCase
from django.conf import settings
from django.urls import reverse
# bootstrap Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'optimap.settings')
django.setup()

from publications.tasks import parse_oai_xml_and_save_publications, harvest_oai_endpoint
from publications.models import Publication, Source, Schedule
from django.contrib.auth import get_user_model

User = get_user_model()


class SimpleTest(TransactionTestCase):
    @classmethod
    @responses.activate
    def setUpClass(cls):
        super().setUpClass()

        # Clear out any publications
        Publication.objects.all().delete()

        # Prepare the two mock HTML article endpoints
        base = os.path.join(settings.BASE_DIR, 'tests', 'harvesting', 'source_1')
        oai_path = os.path.join(base, 'oai_dc.xml')
        art1_path = os.path.join(base, 'article_01.html')
        art2_path = os.path.join(base, 'article_02.html')

        with open(oai_path) as oai,\
             open(art1_path) as a1,\
             open(art2_path) as a2:
            # stub the HTTP fetches that parse_oai_xml_and_save_publications does
            responses.get(
                'http://localhost:8330/index.php/opti-geo/article/view/1',
                body=a1.read()
            )
            responses.get(
                'http://localhost:8330/index.php/opti-geo/article/view/2',
                body=a2.read()
            )

            # run the parser against the OAI XML
            with open(oai_path) as o:
                parse_oai_xml_and_save_publications(o.read(), event=None)

            # mark them as published so the API will expose them
            Publication.objects.all().update(status="p")

    @classmethod
    def tearDownClass(cls):
        Publication.objects.all().delete()
        super().tearDownClass()

    def setUp(self):
        self.client = Client()
        # create a real user for tasks
        self.user = User.objects.create_user(
            username="testuser",
            email="testuser@example.com",
            password="password123"
        )

        # fetch IDs from the API to use in individual‐publication tests
        api = self.client.get('/api/v1/publications/').json()
        fc = api['results']['features']
        if len(fc) >= 2:
            self.id1, self.id2 = fc[1]['id'], fc[0]['id']
        elif len(fc) == 1:
            self.id1 = self.id2 = fc[0]['id']
        else:
            self.id1 = self.id2 = None

    def test_api_root(self):
        resp = self.client.get('/api/v1/publications/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/json')

        results = resp.json()['results']
        self.assertEqual(results['type'], 'FeatureCollection')
        self.assertEqual(len(results['features']), 2)

    def test_api_publication_1(self):
        resp = self.client.get(f'/api/v1/publications/{self.id1}.json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/json')

        body = resp.json()
        self.assertEqual(body['type'], 'Feature')
        geom = body['geometry']
        self.assertEqual(geom['type'], 'GeometryCollection')
        self.assertEqual(geom['geometries'][0]['type'], 'LineString')

        props = body['properties']
        self.assertEqual(props['title'], 'Test 1: One')
        self.assertEqual(props['publicationDate'], '2022-07-01')
        self.assertEqual(props['timeperiod_startdate'], ['2022-06-01'])
        self.assertEqual(
            props['url'],
            'http://localhost:8330/index.php/opti-geo/article/view/1'
        )

    def test_api_publication_2(self):
        resp = self.client.get(f'/api/v1/publications/{self.id2}.json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/json')

        body = resp.json()
        geom = body['geometry']
        self.assertEqual(geom['type'], 'GeometryCollection')
        self.assertEqual(geom['geometries'][0]['type'], 'Polygon')

        props = body['properties']
        self.assertEqual(props['title'], 'Test 2: Two')
        self.assertIsNone(props['doi'])
        self.assertEqual(props['timeperiod_enddate'], ['2022-03-31'])
        self.assertEqual(
            props['url'],
            'http://localhost:8330/index.php/opti-geo/article/view/2'
        )

    def test_task_scheduling(self):
        # Create a Source pointing to the local OAI file
        oai_file = os.path.join(os.getcwd(), 'tests', 'harvesting', 'source_1', 'oai_dc.xml')
        src = Source.objects.create(
            name="Local OAI",
            url_field=f"file://{oai_file}",
            harvest_interval_minutes=60
        )
        # allow the save() hook to schedule
        time.sleep(1)

        sched = Schedule.objects.filter(name=f"Harvest Source {src.id}")
        self.assertTrue(sched.exists(), "Django-Q task not scheduled on save()")

        # run it explicitly
        harvest_oai_endpoint(src.id, self.user)
        count = Publication.objects.count()
        self.assertGreater(count, 0, "harvest_oai_endpoint created no publications")

        # re-parse twice to check deduplication
        with open(oai_file) as f:
            xml = f.read()
        parse_oai_xml_and_save_publications(xml, event=None)
        parse_oai_xml_and_save_publications(xml, event=None)
        self.assertEqual(Publication.objects.count(), count,
                         "Duplicate publications were created!")

        # ensure at least one DOI is valid
        pubs_with_doi = Publication.objects.exclude(doi__isnull=True)
        self.assertTrue(pubs_with_doi.exists())
        for p in pubs_with_doi:
            self.assertTrue(p.doi.startswith("10."),
                            f"DOI is incorrectly formatted: {p.doi}")

    def test_no_duplicates_after_initial_harvest(self):
        # exactly 2 from our sample OAI
        self.assertEqual(Publication.objects.count(), 2)
        resp = self.client.get('/api/v1/publications/')
        feats = resp.json()['results']['features']
        titles = [f['properties']['title'] for f in feats]
        self.assertEqual(len(titles), len(set(titles)),
                         "API returned duplicate feature titles")
