# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for StatisticsSnapshot, SourceCoverageSnapshot, the API endpoint, and the statistics page."""

import datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from works.models import Source, StatisticsSnapshot, Work

User = get_user_model()

STATS_URL = "/api/v1/statistics/"
RECOMPUTE_URL = "/api/v1/statistics/recompute/"
STATS_PAGE_URL = "/statistics/"

_CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "memory": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "dummy": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"},
}


def _country_geom():
    from django.contrib.gis.geos import MultiPolygon, Polygon

    return MultiPolygon(Polygon(((0, 0), (1, 0), (1, 1), (0, 1), (0, 0))))


def _make_published_work(**kwargs):
    return Work.objects.create(status="p", title="Test work", **kwargs)


def _make_source(**kwargs):
    return Source.objects.create(
        name=kwargs.pop("name", "Test Journal"),
        url_field="https://example.com",
        source_type="oai-pmh",
        **kwargs,
    )


@override_settings(CACHES=_CACHES)
class StatisticsSnapshotTests(TestCase):
    """save_statistics_snapshot() creates a correct DB row."""

    def setUp(self):
        # by_topic / by_year read the cached facet helpers; clear between tests
        # so a previous test's snapshot can't leave stale counts behind.
        cache.clear()

    def test_snapshot_created(self):
        _make_published_work()
        from works.utils.statistics import save_statistics_snapshot

        snap = save_statistics_snapshot()
        self.assertIsInstance(snap, StatisticsSnapshot)
        self.assertIsNotNone(snap.computed_at)
        self.assertIsNotNone(snap.next_update)
        self.assertGreaterEqual(snap.published_works, 1)
        self.assertGreaterEqual(snap.total_works, 1)

    def test_snapshot_breakdown_types(self):
        from works.utils.statistics import save_statistics_snapshot

        snap = save_statistics_snapshot()
        self.assertIsInstance(snap.by_continent, list)
        self.assertIsInstance(snap.by_ocean, list)
        self.assertIsInstance(snap.by_country, list)
        self.assertIsInstance(snap.by_publisher, list)
        self.assertIsInstance(snap.by_journal, list)
        self.assertIsInstance(snap.by_topic, list)
        self.assertIsInstance(snap.by_year, list)

    def test_snapshot_by_topic_populated(self):
        _make_published_work(topics=["Hydrology", "Remote Sensing"])
        _make_published_work(topics=["Hydrology"])
        from works.utils.statistics import save_statistics_snapshot

        snap = save_statistics_snapshot()
        counts = {row["name"]: row["count"] for row in snap.by_topic}
        self.assertEqual(counts.get("Hydrology"), 2)
        self.assertEqual(counts.get("Remote Sensing"), 1)

    def test_snapshot_by_year_uses_temporal_coverage(self):
        # by_year is keyed on temporal coverage (data years), NOT publicationDate.
        # A work covering 2019–2021 counts under each of 2019, 2020, 2021.
        _make_published_work(
            publicationDate=datetime.date(2024, 1, 1),
            timeperiod_startdate=["2019-01-01"],
            timeperiod_enddate=["2021-12-31"],
        )
        _make_published_work(
            publicationDate=datetime.date(2024, 1, 1),
            timeperiod_startdate=["2020-01-01"],
            timeperiod_enddate=["2020-12-31"],
        )
        from works.utils.statistics import save_statistics_snapshot

        snap = save_statistics_snapshot()
        counts = {row["year"]: row["count"] for row in snap.by_year}
        self.assertEqual(counts.get(2019), 1)
        self.assertEqual(counts.get(2020), 2)
        self.assertEqual(counts.get(2021), 1)
        # publication year (2024) is NOT a data year here
        self.assertNotIn(2024, counts)
        # ascending chronological order
        years = [row["year"] for row in snap.by_year]
        self.assertEqual(years, sorted(years))

    def test_snapshot_by_journal_populated(self):
        src = _make_source(name="Geo Journal")
        _make_published_work(source=src)
        from works.utils.statistics import save_statistics_snapshot

        snap = save_statistics_snapshot()
        names = [row["name"] for row in snap.by_journal]
        self.assertIn("Geo Journal", names)

    def test_snapshot_by_collection_populated(self):
        from works.models import Collection

        coll = Collection.objects.create(name="Test Coll", identifier="test-coll", is_published=True)
        work = _make_published_work()
        work.collections.add(coll)
        from works.utils.statistics import save_statistics_snapshot

        snap = save_statistics_snapshot()
        names = [row["name"] for row in snap.by_collection]
        self.assertIn("Test Coll", names)
        urls = [row["url"] for row in snap.by_collection]
        self.assertTrue(any("test-coll" in u for u in urls))

    def test_snapshot_by_collection_excludes_unpublished(self):
        from works.models import Collection

        Collection.objects.create(name="Hidden", identifier="hidden-coll", is_published=False)
        from works.utils.statistics import save_statistics_snapshot

        snap = save_statistics_snapshot()
        names = [row["name"] for row in snap.by_collection]
        self.assertNotIn("Hidden", names)

    def test_snapshot_by_country_populated(self):
        from works.models import Country

        de = Country.objects.create(name="Germany", iso_code="DE", geom=_country_geom())
        fr = Country.objects.create(name="France", iso_code="FR", geom=_country_geom())
        _make_published_work().countries.add(de)
        _make_published_work().countries.add(fr)
        from works.utils.statistics import save_statistics_snapshot

        snap = save_statistics_snapshot()
        codes = [row["name"] for row in snap.by_country]
        self.assertIn("DE", codes)
        self.assertIn("FR", codes)

    def test_next_update_is_24h_after_computed(self):
        from works.utils.statistics import save_statistics_snapshot

        snap = save_statistics_snapshot()
        delta = snap.next_update - snap.computed_at
        self.assertAlmostEqual(delta.total_seconds(), 86400, delta=5)


@override_settings(CACHES=_CACHES)
class SourceCoverageSnapshotTests(TestCase):
    """calculate_source_coverage() creates a correct DB row."""

    def test_coverage_computed(self):
        src = _make_source(works_count=100)
        _make_published_work(source=src)
        _make_published_work(source=src)
        from works.utils.statistics import calculate_source_coverage

        snap = calculate_source_coverage(src)
        self.assertEqual(snap.optimap_count, 2)
        self.assertEqual(snap.openalex_total, 100)
        self.assertAlmostEqual(snap.coverage_pct, 2.0)

    def test_coverage_null_when_no_works_count(self):
        src = _make_source(works_count=None)
        from works.utils.statistics import calculate_source_coverage

        snap = calculate_source_coverage(src)
        self.assertIsNone(snap.openalex_total)
        self.assertIsNone(snap.coverage_pct)

    def test_quality_rates_null_when_no_works(self):
        src = _make_source(works_count=None)
        from works.utils.statistics import calculate_source_coverage

        snap = calculate_source_coverage(src)
        self.assertIsNone(snap.spatial_rate)
        self.assertIsNone(snap.temporal_rate)
        self.assertIsNone(snap.open_access_ratio)

    def test_quality_rates_computed(self):
        from django.contrib.gis.geos import GeometryCollection, Point

        src = _make_source(works_count=200)
        _make_published_work(source=src, geometry=GeometryCollection(Point(0, 0)))
        _make_published_work(source=src)
        from works.utils.statistics import calculate_source_coverage

        snap = calculate_source_coverage(src)
        self.assertEqual(snap.optimap_count, 2)
        self.assertEqual(snap.spatial_rate, 50.0)

    def test_by_year_populated(self):
        import datetime

        src = _make_source(works_count=10)
        _make_published_work(source=src, publicationDate=datetime.date(2023, 5, 1))
        _make_published_work(source=src, publicationDate=datetime.date(2024, 3, 1))
        from works.utils.statistics import calculate_source_coverage

        snap = calculate_source_coverage(src)
        years = [entry["year"] for entry in snap.by_year]
        self.assertIn(2023, years)
        self.assertIn(2024, years)

    def test_by_year_empty_when_no_dates(self):
        src = _make_source(works_count=10)
        _make_published_work(source=src)
        from works.utils.statistics import calculate_source_coverage

        snap = calculate_source_coverage(src)
        self.assertEqual(snap.by_year, [])


@override_settings(CACHES=_CACHES, CACHE_MIDDLEWARE_ALIAS="dummy")
class StatisticsAPITests(TestCase):
    """GET /api/v1/statistics/ returns expected shape."""

    def setUp(self):
        from django.core.cache import caches

        for alias in _CACHES:
            caches[alias].clear()

    def test_returns_200(self):
        resp = self.client.get(STATS_URL)
        self.assertEqual(resp.status_code, 200)

    def test_contains_required_keys(self):
        resp = self.client.get(STATS_URL)
        data = resp.json()
        for key in (
            "total_works",
            "published_works",
            "sources",
            "collections",
            "works_by_status",
            "by_continent",
            "by_ocean",
            "by_country",
            "by_publisher",
            "by_journal",
            "total_works_for_user",
            "computed_at",
            "next_update",
        ):
            self.assertIn(key, data, f"missing key: {key}")

    def test_computed_at_null_when_no_snapshot(self):
        resp = self.client.get(STATS_URL)
        self.assertIsNone(resp.json()["computed_at"])

    def test_computed_at_set_after_snapshot(self):
        from works.utils.statistics import save_statistics_snapshot

        save_statistics_snapshot()
        resp = self.client.get(STATS_URL)
        self.assertIsNotNone(resp.json()["computed_at"])

    def test_now_query_param_no_longer_computes(self):
        # The legacy synchronous ?now trigger is removed: GET is read-only and
        # never creates a snapshot, even for staff.
        staff = User.objects.create_user(username="admin", password="pw", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(STATS_URL + "?now")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(StatisticsSnapshot.objects.count(), 0)

    @mock.patch("works.viewsets.async_task")
    def test_recompute_forbidden_for_anonymous(self, mock_async):
        resp = self.client.post(RECOMPUTE_URL)
        self.assertEqual(resp.status_code, 403)
        mock_async.assert_not_called()

    @mock.patch("works.viewsets.async_task")
    def test_recompute_forbidden_for_non_staff(self, mock_async):
        user = User.objects.create_user(username="regular", password="pw")
        self.client.force_login(user)
        resp = self.client.post(RECOMPUTE_URL)
        self.assertEqual(resp.status_code, 403)
        mock_async.assert_not_called()

    @mock.patch("works.viewsets.async_task")
    def test_recompute_schedules_task_for_staff(self, mock_async):
        from django_q.humanhash import humanize

        mock_async.return_value = "ab12cd34ef56ab12cd34ef56ab12cd34"
        staff = User.objects.create_user(username="admin", password="pw", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.post(RECOMPUTE_URL)
        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertTrue(data["scheduled"])
        self.assertEqual(data["task_id"], "ab12cd34ef56ab12cd34ef56ab12cd34")
        # Human-readable name is deterministically derived from the task id.
        self.assertEqual(data["task_name"], humanize("ab12cd34ef56ab12cd34ef56ab12cd34"))
        mock_async.assert_called_once_with("works.tasks.recompute_statistics_snapshot")
        # The job is enqueued, not run synchronously.
        self.assertEqual(StatisticsSnapshot.objects.count(), 0)

    @mock.patch("works.viewsets.async_task")
    def test_recompute_rejects_get(self, mock_async):
        staff = User.objects.create_user(username="admin", password="pw", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(RECOMPUTE_URL)
        self.assertEqual(resp.status_code, 405)
        mock_async.assert_not_called()

    def test_total_works_for_user_non_staff(self):
        _make_published_work()
        Work.objects.create(status="h", title="Harvested")
        resp = self.client.get(STATS_URL)
        data = resp.json()
        self.assertEqual(data["total_works_for_user"], data["published_works"])

    def test_total_works_for_user_staff(self):
        _make_published_work()
        Work.objects.create(status="h", title="Harvested")
        staff = User.objects.create_user(username="admin2", password="pw", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(STATS_URL)
        data = resp.json()
        self.assertEqual(data["total_works_for_user"], data["total_works"])


@override_settings(CACHES=_CACHES)
class StatisticsPageTests(TestCase):
    """GET /statistics/ renders the page."""

    def test_page_renders_without_snapshot(self):
        resp = self.client.get(STATS_PAGE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Statistics")

    def test_page_renders_with_snapshot(self):
        from works.utils.statistics import save_statistics_snapshot

        save_statistics_snapshot()
        resp = self.client.get(STATS_PAGE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Last computed")
