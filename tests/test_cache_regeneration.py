import time
from django.test import TestCase
from django.core.serializers import serialize
from django.contrib.gis.geos import GEOSGeometry
from publications.models import Publication

class GeoJSONCacheRegenerationTestCase(TestCase):
    """
    Test case to verify that updating a Publication correctly regenerates the GeoJSON cache.
    """

    def setUp(self):
        """
        Create two Publication instances with dummy data.
        Note: the geometry field requires a GeometryCollection, so we wrap a point in a GEOMETRYCOLLECTION.
        """
        self.pub1 = Publication.objects.create(
            title="Harvested Article One",
            status="p",
            creationDate="2025-03-31T18:18:21.371Z",
            lastUpdate="2025-03-31T18:18:21.371Z",
            doi="10.5555/harv1",
            source="Harvest Source",
            publicationDate="2010-01-01",
            abstract="This is a harvested article with point geometry.",
            url="http://example.com/harv1",
            timeperiod_startdate=["2009-01-01"],
            timeperiod_enddate=["2010-12-31"],
            geometry=GEOSGeometry('SRID=4326;GEOMETRYCOLLECTION(POINT(7.59573 51.96944))')
        )
        self.pub2 = Publication.objects.create(
            title="Harvested Article Two",
            status="p",
            creationDate="2025-03-31T18:18:21.390Z",
            lastUpdate="2025-03-31T18:18:21.390Z",
            doi="10.5555/harv2",
            source="Harvest Source",
            publicationDate="2011-02-02",
            abstract="This is a harvested article with polygon geometry.",
            url="http://example.com/harv2",
            timeperiod_startdate=["2010-02-01"],
            timeperiod_enddate=["2011-11-30"],
            geometry=GEOSGeometry('SRID=4326;GEOMETRYCOLLECTION(POINT(8.59573 52.96944))')
        )
        # Generate the initial GeoJSON output from all Publication records.
        self.initial_geojson = serialize('geojson', Publication.objects.all(), geometry_field='geometry').encode('utf-8')
        print("Initial GeoJSON output:", self.initial_geojson)

    def regenerate_cache(self):
        """
        Simulate the regeneration of the GeoJSON cache.
        In production, this might be triggered via a management command or signal.
        Here, we re-serialize the Publication queryset.
        """
        geojson = serialize('geojson', Publication.objects.all(), geometry_field='geometry').encode('utf-8')
        print("New GeoJSON output:", geojson)
        return geojson

    def test_cache_regeneration_after_update(self):
        """
        Update a Publication and verify that the regenerated GeoJSON output is different.
        """
        initial_content = self.regenerate_cache()
        self.pub1.title = "Updated Article One"
        self.pub1.save()
        time.sleep(1)
        new_content = self.regenerate_cache()

        print("Initial content before update:", initial_content)
        print("New content after update:", new_content)

        self.assertNotEqual(
            initial_content, new_content,
            "GeoJSON cache was not regenerated after updating a Publication."
        )
