# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the staff-only /manage/sources/ area (issues #133, #44)."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from works.models import HarvestingEvent, Source, Work

User = get_user_model()


def _make_source(**overrides):
    defaults = dict(name="Test Source", url_field="http://example.org/oai", source_type="oai-pmh")
    defaults.update(overrides)
    return Source.objects.create(**defaults)


class ManageStaffGatingTest(TestCase):
    """Every /manage/ page must redirect anonymous and non-staff users."""

    def setUp(self):
        self.client = Client()
        self.source = _make_source()

    def _assert_gated(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302, f"anonymous request to {url} should redirect")

        non_staff = User.objects.create_user(username="plain@example.com", email="plain@example.com", password="p")
        self.client.force_login(non_staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302, f"non-staff request to {url} should redirect")
        self.client.logout()

    def test_dashboard_gated(self):
        self._assert_gated(reverse("optimap:manage-dashboard"))

    def test_sources_list_gated(self):
        self._assert_gated(reverse("optimap:manage-sources"))

    def test_source_detail_gated(self):
        self._assert_gated(reverse("optimap:manage-source-detail", args=[self.source.id]))

    def test_harvests_list_gated(self):
        self._assert_gated(reverse("optimap:manage-harvests"))

    def test_tasks_list_gated(self):
        self._assert_gated(reverse("optimap:manage-tasks"))


class ManageSourcesListTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff@example.com", email="staff@example.com", password="p", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.staff)

    def test_list_renders_sources(self):
        _make_source(name="Alpha Source")
        response = self.client.get(reverse("optimap:manage-sources"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alpha Source")

    def test_filter_by_source_type(self):
        _make_source(name="OAI One", source_type="oai-pmh")
        _make_source(name="RSS One", url_field="http://example.org/feed", source_type="rss")
        response = self.client.get(reverse("optimap:manage-sources"), {"source_type": "rss"})
        self.assertContains(response, "RSS One")
        self.assertNotContains(response, "OAI One")


class ManageSourceCreateEditDeleteTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff2@example.com", email="staff2@example.com", password="p", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.staff)

    def test_create_source(self):
        response = self.client.post(
            reverse("optimap:manage-sources"),
            {"name": "New Source", "url_field": "http://example.org/new", "source_type": "rss"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Source.objects.filter(name="New Source").exists())

    def test_create_source_requires_name_and_url(self):
        response = self.client.post(reverse("optimap:manage-sources"), {"name": "", "url_field": ""})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Source.objects.filter(name="").exists())

    def test_edit_source_updates_fields(self):
        source = _make_source(name="Before")
        response = self.client.post(
            reverse("optimap:manage-source-detail", args=[source.id]),
            {
                "name": "After",
                "url_field": source.url_field,
                "source_type": source.source_type,
                "harvest_interval_minutes": "30",
            },
        )
        self.assertEqual(response.status_code, 302)
        source.refresh_from_db()
        self.assertEqual(source.name, "After")
        self.assertEqual(source.harvest_interval_minutes, 30)

    def test_edit_source_with_non_numeric_interval_does_not_crash(self):
        # Regression: int(request.POST.get("harvest_interval_minutes")) raised
        # an uncaught ValueError on non-numeric input instead of falling back.
        source = _make_source(name="Bad Interval")
        response = self.client.post(
            reverse("optimap:manage-source-detail", args=[source.id]),
            {
                "name": source.name,
                "url_field": source.url_field,
                "source_type": source.source_type,
                "harvest_interval_minutes": "not-a-number",
            },
        )
        self.assertEqual(response.status_code, 302)
        source.refresh_from_db()
        self.assertEqual(source.harvest_interval_minutes, 0)

    def test_create_refuses_duplicate_url_without_confirmation(self):
        _make_source(name="Existing", url_field="http://example.org/dup")
        response = self.client.post(
            reverse("optimap:manage-sources"),
            {"name": "Existing Again", "url_field": "http://example.org/dup", "source_type": "rss"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Source.objects.filter(name="Existing Again").exists())

    def test_create_refuses_duplicate_issn_l_without_confirmation(self):
        _make_source(name="Existing", url_field="http://example.org/a", issn_l="1234-5678")
        response = self.client.post(
            reverse("optimap:manage-sources"),
            {
                "name": "Different name",
                "url_field": "http://example.org/b",
                "source_type": "rss",
                "issn_l": "1234-5678",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Source.objects.filter(name="Different name").exists())

    def test_create_proceeds_with_confirm_duplicate(self):
        _make_source(name="Existing", url_field="http://example.org/dup2")
        response = self.client.post(
            reverse("optimap:manage-sources"),
            {
                "name": "Existing Again",
                "url_field": "http://example.org/dup2",
                "source_type": "rss",
                "confirm_duplicate": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Source.objects.filter(name="Existing Again").exists())

    def test_create_with_no_matching_field_proceeds_normally(self):
        _make_source(name="Unrelated", url_field="http://example.org/unrelated")
        response = self.client.post(
            reverse("optimap:manage-sources"),
            {"name": "Brand New", "url_field": "http://example.org/brand-new", "source_type": "rss"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Source.objects.filter(name="Brand New").exists())


class ManageSourceDuplicateCheckJsonTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="dupcheck@example.com", email="dupcheck@example.com", password="p", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.staff)

    def test_reports_matching_source_by_url(self):
        existing = _make_source(name="Matched", url_field="http://example.org/match")
        response = self.client.get(
            reverse("optimap:manage-source-new"),
            {"format": "json", "check_duplicate": "1", "url_field": "http://example.org/match"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["matches"]), 1)
        self.assertEqual(data["matches"][0]["id"], existing.id)

    def test_no_matches_returns_empty_list(self):
        response = self.client.get(
            reverse("optimap:manage-source-new"),
            {"format": "json", "check_duplicate": "1", "url_field": "http://example.org/nothing-here"},
        )
        self.assertEqual(response.json()["matches"], [])


class ManageSourceOpenalexSuggestJsonTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="oasuggest@example.com", email="oasuggest@example.com", password="p", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.staff)

    @patch("works.harvesting.openalex_discovery.search_openalex_sources")
    def test_openalex_search_returns_candidates(self, mock_search):
        mock_search.return_value = [{"openalex_id": "S123", "name": "Example Journal", "issn_l": "1234-5678"}]
        response = self.client.get(
            reverse("optimap:manage-source-new"), {"format": "json", "openalex_search": "Example Journal"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["candidates"][0]["openalex_id"], "S123")
        mock_search.assert_called_once_with("Example Journal")

    @patch("works.harvesting.openalex_discovery.guess_doi_prefix")
    def test_openalex_doi_prefix_guess(self, mock_guess):
        mock_guess.return_value = "10.1190"
        response = self.client.get(
            reverse("optimap:manage-source-new"), {"format": "json", "openalex_doi_prefix": "S123"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["doi_prefix"], "10.1190")
        mock_guess.assert_called_once_with("S123")

    def test_delete_source_without_works_succeeds(self):
        source = _make_source(name="Deletable")
        response = self.client.post(reverse("optimap:manage-source-delete", args=[source.id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Source.objects.filter(id=source.id).exists())

    def test_delete_source_with_works_is_refused(self):
        source = _make_source(name="Has Works")
        Work.objects.create(title="A Work", source=source, doi="10.1234/manage-del-1")
        response = self.client.post(reverse("optimap:manage-source-delete", args=[source.id]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Source.objects.filter(id=source.id).exists())

    def test_source_detail_shows_recent_events_and_works(self):
        source = _make_source(name="Detailed")
        HarvestingEvent.objects.create(source=source, status="completed", trigger_source="ui")
        Work.objects.create(title="Harvested Work", source=source, doi="10.1234/manage-det-1")
        response = self.client.get(reverse("optimap:manage-source-detail", args=[source.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Harvested Work")


class ManageNeverCachedTest(TestCase):
    """Every /manage/ page must opt out of the site-wide UpdateCacheMiddleware
    (CACHE_MIDDLEWARE_SECONDS=3600 by default) and tell the browser not to
    cache it either — otherwise a poll or a plain refresh can silently serve
    a stale copy instead of the current state."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username="cache-staff@example.com", email="cache-staff@example.com", password="p", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.staff)
        self.source = _make_source()
        self.event = HarvestingEvent.objects.create(source=self.source, status="in_progress", trigger_source="ui")

    def _assert_never_cached(self, url):
        response = self.client.get(url)
        cache_control = response.get("Cache-Control", "")
        self.assertIn("no-store", cache_control, f"{url} is missing no-store — it can be cached")
        self.assertIn("no-cache", cache_control, f"{url} is missing no-cache — it can be cached")

    def test_dashboard_never_cached(self):
        self._assert_never_cached(reverse("optimap:manage-dashboard"))

    def test_dashboard_json_poll_never_cached(self):
        self._assert_never_cached(reverse("optimap:manage-dashboard") + "?format=json")

    def test_sources_list_never_cached(self):
        self._assert_never_cached(reverse("optimap:manage-sources"))

    def test_harvests_list_never_cached(self):
        self._assert_never_cached(reverse("optimap:manage-harvests"))

    def test_harvest_detail_never_cached(self):
        self._assert_never_cached(reverse("optimap:manage-harvest-detail", args=[self.event.id]))

    def test_harvest_detail_json_poll_never_cached(self):
        url = reverse("optimap:manage-harvest-detail", args=[self.event.id]) + "?format=json&since=0"
        self._assert_never_cached(url)

    def test_tasks_list_never_cached(self):
        self._assert_never_cached(reverse("optimap:manage-tasks"))


class ManageDashboardEmptyReferenceTableWarningTest(TestCase):
    """The dashboard flags an empty Country/GlobalRegion table — otherwise the
    per-harvest country/region point-in-polygon join (works.signals) silently
    links nothing, with no error anywhere, which looks like the enrichment
    itself is broken rather than a one-time missing setup step.
    """

    def setUp(self):
        self.staff = User.objects.create_user(
            username="ref-staff@example.com", email="ref-staff@example.com", password="p", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.staff)

    def test_warning_shown_when_both_tables_empty(self):
        response = self.client.get(reverse("optimap:manage-dashboard"))
        self.assertContains(response, "load_countries")
        self.assertContains(response, "load_global_regions")

    def test_warning_hidden_when_both_tables_populated(self):
        from django.contrib.gis.geos import GEOSGeometry

        from works.models import Country, GlobalRegion

        poly = GEOSGeometry("MULTIPOLYGON(((0 0, 0 1, 1 1, 1 0, 0 0)))", srid=4326)
        Country.objects.create(iso_code="XX", name="Testland", geom=poly)
        GlobalRegion.objects.create(name="Test Continent", region_type=GlobalRegion.CONTINENT, geom=poly)

        response = self.client.get(reverse("optimap:manage-dashboard"))
        self.assertNotContains(response, "load_countries")
        self.assertNotContains(response, "load_global_regions")

    def test_only_country_warning_shown_when_only_regions_populated(self):
        from django.contrib.gis.geos import GEOSGeometry

        from works.models import GlobalRegion

        poly = GEOSGeometry("MULTIPOLYGON(((0 0, 0 1, 1 1, 1 0, 0 0)))", srid=4326)
        GlobalRegion.objects.create(name="Test Continent", region_type=GlobalRegion.CONTINENT, geom=poly)

        response = self.client.get(reverse("optimap:manage-dashboard"))
        self.assertContains(response, "load_countries")
        self.assertNotContains(response, "load_global_regions")
