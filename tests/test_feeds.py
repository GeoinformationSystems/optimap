import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

import xml.etree.ElementTree as ET
from django.test import TestCase
from django.contrib.gis.geos import Point, LineString, Polygon, GeometryCollection
from datetime import datetime
from publications.models import Publication

from xmldiff import main as xmldiff_main
from xmldiff import formatting

class GeoFeedTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        """ Set up test publications with geospatial data """
        Publication.objects.all().delete()
        cls.pub1 = Publication.objects.create(
            title="Point Test",
            abstract="Publication with a single point inside a collection.",
            url="https://example.com/point",
            status="p",
            publicationDate=datetime(2023, 5, 10),
            doi="10.1234/test-doi-1",
            geometry=GeometryCollection(Point(12.4924, 41.8902))  
        )

        cls.pub2 = Publication.objects.create(
            title="Polygon Test",
            abstract="Publication with a polygon inside a collection.",
            url="https://example.com/polygon",
            status="p",
            publicationDate=datetime(2023, 5, 15),
            doi="10.1234/test-doi-2",
            geometry=GeometryCollection(Polygon([
                (10.0, 50.0), (11.0, 51.0), (12.0, 50.0), (10.0, 50.0)
            ])) 
        )

        cls.pub3 = Publication.objects.create(
            title="LineString Test",
            abstract="Publication with a linestring inside a collection.",
            url="https://example.com/linestring",
            status="p",
            publicationDate=datetime(2023, 5, 20),
            doi="10.1234/test-doi-3",
            geometry=GeometryCollection(LineString([(5.0, 45.0), (6.0, 46.0), (7.0, 45.5)])) 
        )

    def _fetch_feed(self, feed_type):
        """ Helper function to fetch RSS/Atom feed content """
        feed_urls = {
            "georss": "/feed/georss/",
            "geoatom": "/feed/geoatom/",
        }

        if feed_type not in feed_urls:
            self.fail(f"Invalid feed type requested: {feed_type}")

        url = feed_urls[feed_type]
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200, f"Feed {feed_type} did not return 200")

        xml_content = response.content.decode('utf-8')

        return xml_content

    def _compare_with_reference(self, generated_xml, filename):
        """ Save and compare generated XML with reference file """
        reference_path = os.path.join(os.path.dirname(__file__), "reference", filename)

        if not os.path.exists(reference_path):
            os.makedirs(os.path.dirname(reference_path), exist_ok=True)
            with open(reference_path, "w", encoding="utf-8") as f:
                f.write(generated_xml)
            return  

        with open(reference_path, "r", encoding="utf-8") as f:
            reference_xml = f.read()

        diff = xmldiff_main.diff_texts(
            reference_xml.encode("utf-8"),
            generated_xml.encode("utf-8"),
            formatter=formatting.DiffFormatter()
        )

        if diff:
            self.fail(f"{filename} does not match reference!\n\nDiff:\n{diff}")

    def _extract_namespaces(self, xml_content):
        """ Extract namespaces dynamically from XML content """
        try:
            from io import StringIO
            xml_buffer = StringIO(xml_content) 
            
            namespace_map = {}
            for event, (prefix, uri) in ET.iterparse(xml_buffer, events=['start-ns']):
                namespace_map[prefix] = uri

            return namespace_map
        except ET.ParseError as error:
            print("XML Parsing Error:", str(error))
            return {}

    def _parse_xml(self, xml_content):
        """ Parse XML while handling namespace prefixes """
        try:
            return ET.ElementTree(ET.fromstring(xml_content))
        except ET.ParseError as e:
            print("XML Parse Error:", str(e))
            self.fail("Invalid XML response! Check namespace prefixes.")

    def test_georss_feed(self):
        """ Test GeoRSS feed structure and content """
        georss_xml = self._fetch_feed("georss")
        self._compare_with_reference(georss_xml, "expected_georss.xml")

        root = self._parse_xml(georss_xml).getroot()
        namespace = self._extract_namespaces(georss_xml) 

        points = root.findall(".//georss:point", namespaces=namespace)
        self.assertEqual(len(points), 1, "Expected at least one <georss:point> element")

    def test_geoatom_feed(self):
        """ Test GeoAtom feed structure and content """
        geoatom_xml = self._fetch_feed("geoatom")
        self._compare_with_reference(geoatom_xml, "expected_geoatom.xml")

        root = self._parse_xml(geoatom_xml).getroot()
        namespace = self._extract_namespaces(geoatom_xml)  

        latitudes = root.findall(".//geo:lat", namespaces=namespace)
        longitudes = root.findall(".//geo:long", namespaces=namespace)
        self.assertGreaterEqual(len(latitudes), 1, "Expected at least one <geo:lat> element")

        # Validate content of the fields
        expected_lat = "41.8902"
        expected_lon = "12.4924"

        lat_texts = [lat.text for lat in latitudes]
        lon_texts = [lon.text for lon in longitudes]

        self.assertIn(expected_lat, lat_texts, f"Expected latitude '{expected_lat}' not found in feed")
        self.assertIn(expected_lon, lon_texts, f"Expected longitude '{expected_lon}' not found in feed")
