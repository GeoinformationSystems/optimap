import os
import json
import tempfile
from django.test import TestCase
from django.core.serializers import serialize
from osgeo import ogr
from publications.models import Publication
from publications.views import generate_geopackage
# For GeoJSON generation the view uses Django's serializer directly,
from publications.tasks import regenerate_geojson_cache


class GeoDataTestCase(TestCase):
    def setUp(self):
        """
        Create a few Publication objects with minimal data.
        The geometry field is assumed to be set with a valid WKT string.
        Adjust the fields as needed to reflect your model's requirements.
        """
        # We use a simple point geometry in a GeometryCollection.
        wkt_point1 = "GEOMETRYCOLLECTION(POINT(7.59573 51.96944))"
        wkt_point2 = "GEOMETRYCOLLECTION(POINT(8.59573 52.96944))"
        wkt_point3 = "GEOMETRYCOLLECTION(POINT(9.59573 53.96944))"

        Publication.objects.create(
            title="Publication One",
            abstract="Abstract of publication one.",
            publicationDate="2020-01-01",
            url="http://example.com/1",
            source="Source One",
            doi="10.0001/one",
            geometry=wkt_point1,  
            timeperiod_startdate=["2020-01-01"],
            timeperiod_enddate=["2020-12-31"],
        )

        Publication.objects.create(
            title="Publication Two",
            abstract="Abstract of publication two.",
            publicationDate="2020-06-01",
            url="http://example.com/2",
            source="Source Two",
            doi="10.0001/two",
            geometry=wkt_point2,
            timeperiod_startdate=["2020-06-01"],
            timeperiod_enddate=["2020-12-31"],
        )

        Publication.objects.create(
            title="Publication Three",
            abstract="Abstract of publication three.",
            publicationDate="2020-09-01",
            url="http://example.com/3",
            source="Source Three",
            doi="10.0001/three",
            geometry=wkt_point3,
            timeperiod_startdate=["2020-09-01"],
            timeperiod_enddate=["2020-12-31"],
        )

    def test_geojson_generation(self):
        """
        Verify that GeoJSON output from serializing Publication objects:
         - Is non-empty.
         - Has the proper FeatureCollection structure.
         - Contains the same number of features as there are Publication objects.
        """
        geojson_data = serialize('geojson', Publication.objects.all(), geometry_field='geometry')
        self.assertTrue(len(geojson_data) > 0, "GeoJSON data should not be empty")

        geojson_obj = json.loads(geojson_data)
        self.assertEqual(geojson_obj.get("type"), "FeatureCollection", "GeoJSON type should be FeatureCollection")
        features = geojson_obj.get("features", [])
        self.assertEqual(len(features), Publication.objects.count(),
                         "Number of GeoJSON features should match the number of Publication records")

        # Optionally, verify one of the feature's properties.
        self.assertIn("title", features[0]["properties"], "GeoJSON feature should have a 'title' property")

    def test_geopackage_generation(self):
        """
        Verify that the GeoPackage is generated correctly:
         - The generated byte data is non-empty.
         - The file can be opened with GDAL/OGR.
         - The file contains the layer 'publications' with the correct number of features.
        """
        gpkg_data = generate_geopackage()
        self.assertTrue(len(gpkg_data) > 0, "Generated GeoPackage data should not be empty")

        with tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False) as tmp_file:
            tmp_file.write(gpkg_data)
            temp_filename = tmp_file.name

        try:
            datasource = ogr.Open(temp_filename)
            self.assertIsNotNone(datasource, "GDAL/OGR should be able to open the GeoPackage file")

            layer = datasource.GetLayerByName("publications")
            self.assertIsNotNone(layer, "Layer 'publications' should be found in the GeoPackage")

            feature_count = layer.GetFeatureCount()
            self.assertEqual(feature_count, Publication.objects.count(),
                             "Feature count in GeoPackage should match the Publication count")
        finally:
            os.remove(temp_filename)

    def test_update_reflects_in_generated_data(self):
        """
        This test ensures that updating a Publication record modifies the generated GeoJSON and GeoPackage.
         - Generate initial GeoJSON and GeoPackage.
         - Update a publication (e.g., change the title).
         - Generate the outputs again.
         - Assert that the new outputs are different from the old ones.
        """
        initial_geojson = serialize('geojson', Publication.objects.all(), geometry_field='geometry')
        initial_gpkg = generate_geopackage()

        pub = Publication.objects.first()
        old_title = pub.title
        pub.title = old_title + " Updated"
        pub.save()

        updated_geojson = serialize('geojson', Publication.objects.all(), geometry_field='geometry')
        updated_gpkg = generate_geopackage()

        self.assertNotEqual(initial_geojson, updated_geojson,
                            "GeoJSON data should update when a Publication changes")
        self.assertNotEqual(initial_gpkg, updated_gpkg,
                            "GeoPackage data should update when a Publication changes")
