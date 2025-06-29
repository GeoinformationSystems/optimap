import json
from django.test import TransactionTestCase
from rest_framework.test import APIClient
from rest_framework import status
from publications.models import Source, Publication

class SourceAPITest(TransactionTestCase):
    """
    Tests for the source endpoints:
      - GET /api/v1/sources/
      - GET /api/v1/sources/{pk}/
    """

    def setUp(self):
        self.client = APIClient()

        # 1. Fully populated source (works_api_url is now a property, not a field)
        self.srcA = Source.objects.create(
            name="Test source A",
            issn_l="1234-5678",
            openalex_id="https://openalex.org/S012345678",
            openalex_url="https://openalex.org/S012345678",
            publisher_name="Test Publisher A",
            works_count=42,
        )

        # 2. Source missing optional fields
        self.srcB = Source.objects.create(
            name="No ISSN source",
            issn_l=None,
            openalex_id=None,
            openalex_url=None,
            publisher_name=None,
            works_count=None,
        )

    def test_list_sources(self):
        """
        GET /api/v1/sources/ should return at least two sources,
        and each result must include the eight expected fields.
        """
        url = "/api/v1/sources/"
        response = self.client.get(url, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()

        # Unwrap pagination if present
        if isinstance(data, dict) and "results" in data:
            sources_list = data["results"]
        else:
            sources_list = data

        # We expect exactly 2 sources
        self.assertEqual(len(sources_list), 2)
        names = {j["name"] for j in sources_list}
        self.assertIn("Test source A", names)
        self.assertIn("No ISSN source", names)

        # Verify all eight fields for the populated source
        populated = next(x for x in sources_list if x["name"] == "Test source A")
        for key in [
            "id",
            "name",
            "issn_l",
            "openalex_id",
            "openalex_url",
            "publisher_name",
            "works_count",
            "works_api_url",
        ]:
            self.assertIn(key, populated)

        # Verify the second source has None (null) for optional fields
        no_issn = next(x for x in sources_list if x["name"] == "No ISSN source")
        self.assertIsNone(no_issn["issn_l"])
        self.assertIsNone(no_issn["openalex_id"])
        self.assertIsNone(no_issn["publisher_name"])
        self.assertIsNone(no_issn["works_count"])
        self.assertIsNone(no_issn["works_api_url"])

    def test_retrieve_source_details(self):
        """
        GET /api/v1/sources/{pk}/ should return the correct fields for that source.
        """
        src = Source.objects.get(name="Test source A")
        url = f"/api/v1/sources/{src.pk}/"
        response = self.client.get(url, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        jdata = response.json()
        self.assertEqual(jdata["id"], src.pk)
        self.assertEqual(jdata["name"], src.name)
        self.assertEqual(jdata["issn_l"], src.issn_l)
        self.assertEqual(jdata["openalex_id"], src.openalex_id)
        self.assertEqual(jdata["openalex_url"], src.openalex_url)
        self.assertEqual(jdata["publisher_name"], src.publisher_name)
        self.assertEqual(jdata["works_count"], src.works_count)
        self.assertEqual(jdata["works_api_url"], src.works_api_url)


class PublicationAPITest(TransactionTestCase):
    """
    Tests for the Publication endpoints:
      - GET /api/v1/publications/
      - Filtering by ?source_id={pk}
      - Ensure nested 'source_details' appears with its fields.
    """

    def setUp(self):
        self.client = APIClient()

        # 1. Create one source to attach to a Publication
        self.src = Source.objects.create(
            name="API source",
            issn_l="1111-2222",
            openalex_id="https://openalex.org/S011112222",
            openalex_url="https://openalex.org/S011112222",
            publisher_name="API Publisher",
            works_count=7,
        )

        # 2. Create a published Publication linked to that source
        Publication.objects.create(
            title="API Paper",
            abstract="Testing nested source_details serialization",
            publicationDate="2021-01-01",
            doi="10.1000/testdoi",
            url="http://example.com/api-paper",
            geometry=None,  # No geometry for test convenience
            source=self.src,
            timeperiod_startdate=["2020-01-01"],
            timeperiod_enddate=["2021-01-01"],
            provenance="Test provenance",
            status="p"
        )

    def _unwrap_publications(self, data):
        """
        Given JSON from /api/v1/publications/, return a list of property‐dicts.

        Handles:
          1. Paginated GeoJSON: data["results"] is a dict (FeatureCollection).
          2. Ungrouped GeoJSON: data["features"] directly.
          3. Simple list of dicts (fall back).
        """
        # Case 1: Paginated—"results" holds a FeatureCollection dict
        if isinstance(data, dict) and "results" in data:
            results_block = data["results"]
            if not isinstance(results_block, dict):
                self.fail(f"Expected 'results' to be a dict containing FeatureCollection, got {type(results_block).__name__}")
            # Now expect results_block["features"] to be a list
            if "features" not in results_block or not isinstance(results_block["features"], list):
                self.fail(f"Expected 'features' list inside paginated 'results', but got: {results_block}")
            return [feat["properties"] for feat in results_block["features"]]

        # Case 2: Unpaginated GeoJSON—top‐level "features"
        if isinstance(data, dict) and "features" in data:
            features_block = data["features"]
            if not isinstance(features_block, list):
                self.fail(f"Expected top‐level 'features' to be a list, but got {type(features_block).__name__}")
            return [feat["properties"] for feat in features_block]

        # Case 3: Plain list (already a list of property‐dicts)
        if isinstance(data, list):
            # If items look like GeoJSON features, unwrap their "properties"
            if len(data) > 0 and isinstance(data[0], dict) and "properties" in data[0]:
                return [item["properties"] for item in data]
            return data

        # Anything else is unexpected
        self.fail(f"Unexpected JSON structure for publications endpoint: {type(data).__name__}")

    def test_publication_includes_source_details(self):
        """
        GET /api/v1/publications/ should return ≥1 publication,
        and each publication’s 'source_details' must include all eight source fields.
        """
        url = "/api/v1/publications/"
        response = self.client.get(url, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        pubs_list = self._unwrap_publications(data)
        self.assertEqual(len(pubs_list), 1)

        pub_data = pubs_list[0]
        self.assertIn("source_details", pub_data)
        details = pub_data["source_details"]
        self.assertIsInstance(details, dict)

        # Check that all eight fields appear:
        for key in [
            "id",
            "name",
            "issn_l",
            "openalex_id",
            "openalex_url",
            "publisher_name",
            "works_count",
            "works_api_url",
        ]:
            self.assertIn(key, details)

        # Compare against the source we created
        self.assertEqual(details["id"], self.src.pk)
        self.assertEqual(details["name"], self.src.name)
        self.assertEqual(details["issn_l"], self.src.issn_l)
        self.assertEqual(details["openalex_id"], self.src.openalex_id)
        self.assertEqual(details["openalex_url"], self.src.openalex_url)
        self.assertEqual(details["publisher_name"], self.src.publisher_name)
        self.assertEqual(details["works_count"], self.src.works_count)
        self.assertEqual(details["works_api_url"], self.src.works_api_url)

    def test_filter_publications_by_source(self):
        """
        GET /api/v1/publications/?source_id=<pk> should return only those
        publications whose source_details["id"] equals <pk>.
        """
        url = f"/api/v1/publications/?source_id={self.src.pk}"
        response = self.client.get(url, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        pubs_list = self._unwrap_publications(data)
        self.assertEqual(len(pubs_list), 1)

        # Each publication must have source_details["id"] == source.pk
        for p in pubs_list:
            self.assertIn("source_details", p)
            self.assertIsInstance(p["source_details"], dict)
            self.assertEqual(p["source_details"]["id"], self.src.pk)
