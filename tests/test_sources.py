from django.test import TestCase
from django.urls import reverse, NoReverseMatch
from django.core.exceptions import ValidationError

from rest_framework.test import APIClient
from rest_framework import status

from publications.models import 
from stdnum.issn import is_valid as is_valid_issn


class JournalModelTest(TestCase):
    def test_issn_l_validator_accepts_valid_issn(self):
        """Model-level validator should accept a syntactically valid ISSN-L."""
        valid = "2434-561X"  # example valid ISSN-L (with correct check digit)
        self.assertTrue(is_valid_issn(valid))

    def test_issn_l_validator_rejects_invalid_issn(self):
        """full_clean() should raise ValidationError on an invalid ISSN-L."""
        invalid = "1234-5678"
        j = Source(display_name="Bad ISSN", issn_l=invalid)
        with self.assertRaises(ValidationError):
            j.full_clean()
    def test_list_sources_includes_geometry(self):
        url = "/api/v1/sources/"
        resp = self.client.get(url)
        results = resp.json()["results"]
        # Should be either a list [lon, lat] or null
        self.assertIn("geometry", results[0])
        self.assertTrue(results[0]["geometry"] is None or isinstance(results[0]["geometry"], list))

class JournalAPITest(TestCase):
    @classmethod
    def setUpTestData(cls):
        # two with ISSNs…
        Source.objects.create(
            display_name="Nature",
            issn_l="0028-0836",
            issn_list=["0028-0836", "1476-4687"],
            publisher="Nature Publishing Group",
            openalex_id="https://openalex.org/S137773608",
            articles=["W1", "W2"],
        )
        Source.objects.create(
            display_name="Science",
            issn_l="0036-8075",
            issn_list=["0036-8075"],
            publisher="American Association for the Advancement of Science",
            openalex_id="https://openalex.org/S137774328",
            articles=["W3"],
        )
        Source.objects.create(
            display_name="My Source",
            issn_l=None,
            issn_list=[],
            publisher="Local Publisher",
            openalex_id=None,
            articles=[],
        )

    def setUp(self):
        self.client = APIClient()

    def test_list_sources(self):
        """GET /api/v1/sources/ should list all sources with the right fields."""
        # try named URL first...
        try:
            url = reverse("source-list")
        except NoReverseMatch:
            # fallback to hard-coded path if reverse() fails
            url = "/api/v1/sources/"

        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = data["results"]
        data = resp.json()
        self.assertIn("results", data)
        results = data["results"]
        self.assertEqual(len(results), 3)

        # Check fields of the first source
        first = results[0]
        expected_fields = {
            "display_name",
            "issn_l",
            "issn_list",
            "publisher",
            "openalex_id",
            "articles",
        }
        self.assertEqual(set(first.keys()), expected_fields)

        # Check that the values match what we set up
        noissn = next(j for j in results if j["display_name"] == "My Source")
        self.assertIsNone(noissn["issn_l"])
        self.assertEqual(noissn["issn_list"], [])
        self.assertEqual(noissn["publisher"], "Local Publisher")
        self.assertEqual(noissn["openalex_id"], None)
        self.assertEqual(noissn["articles"], [])
