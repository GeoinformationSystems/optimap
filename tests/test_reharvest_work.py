# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the admin single-work re-harvest feature.

Covers:
- ``works.harvesting.crossref.reharvest_work`` (with OpenAlex / OpenAIRE
  enrichment mocked offline) — refresh-in-place, careful-update preservation,
  no-DOI and not-found handling,
- the ``POST /work/<identifier>/reharvest/`` staff-only endpoint.
"""

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

from unittest.mock import patch

import responses
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import GeometryCollection, Point
from django.core.cache import cache
from django.test import TestCase, override_settings

from works.harvesting.crossref import reharvest_work
from works.models import HarvestingEvent, Work

User = get_user_model()

_CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "memory": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "dummy": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"},
}


def _crossref_single(doi="10.1234/abc123", **overrides):
    """A minimal single-DOI Crossref response body ({"message": {...}})."""
    message = {
        "DOI": doi,
        "URL": f"https://doi.org/{doi}",
        "title": ["Refreshed title"],
        "abstract": "<jats:p>Refreshed abstract.</jats:p>",
        "published": {"date-parts": [[2024, 5, 1]]},
        "author": [{"given": "Ada", "family": "Lovelace"}],
    }
    message.update(overrides)
    return {"message": message}


@override_settings(CACHES=_CACHES, ADMINS=[], EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ReharvestWorkFunctionTests(TestCase):
    def setUp(self):
        cache.clear()
        oa = patch("works.harvesting.crossref.build_openalex_fields", return_value=({}, {}))
        self.mock_oa = oa.start()
        self.addCleanup(oa.stop)
        oaire = patch("works.harvesting.crossref.enrich_work_from_openaire", return_value=None)
        self.mock_oaire = oaire.start()
        self.addCleanup(oaire.stop)
        abstr = patch("works.harvesting.crossref.fetch_copernicus_abstract", return_value=None)
        abstr.start()
        self.addCleanup(abstr.stop)
        # Neutral extraction by default; override per test for the geometry/temporal cases.
        geo = patch("works.harvesting.crossref.extract_geometry_from_html", return_value=(None, None))
        self.mock_geo = geo.start()
        self.addCleanup(geo.stop)
        tmp = patch("works.harvesting.crossref.extract_timeperiod_from_html", return_value=([], []))
        self.mock_tmp = tmp.start()
        self.addCleanup(tmp.stop)

    def _register_landing(self, doi):
        """Register the Crossref API and the (redirected) landing-page URL."""
        responses.add(
            responses.GET,
            f"https://api.crossref.org/works/{doi}",
            json=_crossref_single(doi),
            status=200,
        )
        responses.add(responses.GET, f"https://doi.org/{doi}", body="<html></html>", status=200)

    @responses.activate
    def test_refreshes_metadata_in_place(self):
        self._register_landing("10.1234/abc123")
        work = Work.objects.create(title="Stale title", doi="10.1234/abc123", status="h")
        result, action, info = reharvest_work(work)
        self.assertEqual(action, "updated")
        work.refresh_from_db()
        self.assertEqual(work.title, "Refreshed title")
        # A harvest_update event is appended to provenance.
        events = (work.provenance or {}).get("events", [])
        self.assertTrue(any(e.get("type") == "harvest_update" for e in events))
        self.mock_oaire.assert_called_once()

    @responses.activate
    def test_event_defaults_to_manual_trigger_source(self):
        self._register_landing("10.1234/trigger-src")
        work = Work.objects.create(title="Stale title", doi="10.1234/trigger-src", status="h")
        reharvest_work(work)
        event = HarvestingEvent.objects.filter(source=work.source).latest("started_at")
        self.assertEqual(event.trigger_source, "manual")

    @responses.activate
    def test_overrides_non_user_geometry_from_source(self):
        # Geometry present but never user-contributed → re-harvest overrides it.
        old = GeometryCollection(Point(7.0, 51.0))
        new = GeometryCollection(Point(20.0, 30.0))
        work = Work.objects.create(title="Stale", doi="10.1234/geo", status="p", geometry=old)
        self._register_landing("10.1234/geo")
        self.mock_geo.return_value = (new, "DC.SpatialCoverage")
        self.mock_tmp.return_value = (["2020-01-01"], ["2020-12-31"])

        result, action, info = reharvest_work(work)
        self.assertEqual(action, "updated")
        self.assertEqual(info["geometry"], "updated")
        self.assertEqual(info["temporal"], "updated")
        work.refresh_from_db()
        # Geometry overridden from source; published status still preserved.
        self.assertAlmostEqual(work.geometry[0].x, 20.0)
        self.assertEqual(work.status, "p")
        self.assertEqual(work.timeperiod_startdate, ["2020-01-01"])
        self.assertEqual((work.provenance or {}).get("metadata_sources", {}).get("geometry"), "DC.SpatialCoverage")

    @responses.activate
    def test_preserves_user_contributed_geometry(self):
        # A user contributed the geometry (provenance contribution event) →
        # re-harvest must NOT override it, even though the source has a new one.
        old = GeometryCollection(Point(7.0, 51.0))
        work = Work.objects.create(
            title="Stale",
            doi="10.1234/usergeo",
            status="c",
            geometry=old,
            provenance={"events": [{"type": "contribution", "kinds": ["spatial"], "user_id": 99}]},
        )
        self._register_landing("10.1234/usergeo")
        self.mock_geo.return_value = (GeometryCollection(Point(20.0, 30.0)), "DC.SpatialCoverage")

        result, action, info = reharvest_work(work)
        self.assertEqual(info["geometry"], "preserved_user_contribution")
        work.refresh_from_db()
        # Unchanged — the user's geometry wins.
        self.assertAlmostEqual(work.geometry[0].x, 7.0)

    @responses.activate
    def test_no_source_geometry_keeps_existing(self):
        old = GeometryCollection(Point(7.0, 51.0))
        work = Work.objects.create(title="Stale", doi="10.1234/nogeo", status="p", geometry=old)
        self._register_landing("10.1234/nogeo")
        # extract_geometry_from_html returns nothing (setUp default) → keep existing.
        result, action, info = reharvest_work(work)
        self.assertEqual(info["geometry"], "no_source_value")
        work.refresh_from_db()
        self.assertAlmostEqual(work.geometry[0].x, 7.0)

    @responses.activate
    def test_placeholder_temporal_is_not_treated_as_value(self):
        # extract_timeperiod_from_html returns [None]/[None] (truthy lists) when
        # a page has no dates — must be treated as "no value", not an update.
        work = Work.objects.create(title="Stale", doi="10.1234/notime", status="h")
        self._register_landing("10.1234/notime")
        self.mock_tmp.return_value = ([None], [None])
        result, action, info = reharvest_work(work)
        self.assertEqual(info["temporal"], "no_source_value")
        work.refresh_from_db()
        self.assertNotEqual(work.timeperiod_startdate, [None])

    def test_no_doi_returns_no_doi(self):
        work = Work.objects.create(title="No DOI", doi=None, status="h")
        result, action, info = reharvest_work(work)
        self.assertEqual(action, "no_doi")
        self.assertIsNone(result)

    @responses.activate
    def test_unknown_doi_returns_not_found(self):
        work = Work.objects.create(title="Missing", doi="10.1234/missing", status="h")
        responses.add(
            responses.GET,
            "https://api.crossref.org/works/10.1234/missing",
            status=404,
        )
        result, action, info = reharvest_work(work)
        self.assertEqual(action, "not_found")


@override_settings(CACHES=_CACHES, ADMINS=[], EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ReharvestEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = User.objects.create_user(
            username="s@example.com", email="s@example.com", password="pw12345!", is_staff=True
        )
        self.user = User.objects.create_user(username="u@example.com", email="u@example.com", password="pw12345!")
        self.work = Work.objects.create(title="Stale", doi="10.1234/end", status="h")
        for target in ("build_openalex_fields", "enrich_work_from_openaire", "fetch_copernicus_abstract"):
            default = ({}, {}) if target == "build_openalex_fields" else None
            p = patch(f"works.harvesting.crossref.{target}", return_value=default)
            p.start()
            self.addCleanup(p.stop)
        geo = patch("works.harvesting.crossref.extract_geometry_from_html", return_value=(None, None))
        self.mock_geo = geo.start()
        self.addCleanup(geo.stop)
        tmp = patch("works.harvesting.crossref.extract_timeperiod_from_html", return_value=([], []))
        tmp.start()
        self.addCleanup(tmp.stop)

    def _url(self):
        return f"/work/{self.work.id}/reharvest/"

    def _register_landing(self, doi):
        responses.add(
            responses.GET,
            f"https://api.crossref.org/works/{doi}",
            json=_crossref_single(doi),
            status=200,
        )
        responses.add(responses.GET, f"https://doi.org/{doi}", body="<html></html>", status=200)

    @responses.activate
    def test_staff_can_reharvest(self):
        self._register_landing("10.1234/end")
        self.client.force_login(self.staff)
        resp = self.client.post(self._url(), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        self.work.refresh_from_db()
        self.assertEqual(self.work.title, "Refreshed title")

    @responses.activate
    def test_message_reports_geometry_updated(self):
        self._register_landing("10.1234/end")
        from django.contrib.gis.geos import GeometryCollection, Point

        self.mock_geo.return_value = (GeometryCollection(Point(20.0, 30.0)), "DC.SpatialCoverage")
        self.client.force_login(self.staff)
        resp = self.client.post(self._url(), content_type="application/json")
        self.assertIn("Geometry updated from source", resp.json()["message"])

    @responses.activate
    def test_message_reports_user_contributed_geometry_preserved(self):
        from django.contrib.gis.geos import GeometryCollection, Point

        w = Work.objects.create(
            title="Stale",
            doi="10.1234/usr",
            status="c",
            geometry=GeometryCollection(Point(7.0, 51.0)),
            provenance={"events": [{"type": "contribution", "kinds": ["spatial"]}]},
        )
        self._register_landing("10.1234/usr")
        self.mock_geo.return_value = (GeometryCollection(Point(20.0, 30.0)), "DC.SpatialCoverage")
        self.client.force_login(self.staff)
        resp = self.client.post(f"/work/{w.id}/reharvest/", content_type="application/json")
        self.assertIn("preserved (user-contributed", resp.json()["message"])
        w.refresh_from_db()
        self.assertAlmostEqual(w.geometry[0].x, 7.0)

    def test_non_staff_forbidden(self):
        self.client.force_login(self.user)
        resp = self.client.post(self._url(), content_type="application/json")
        # staff_member_required redirects to the admin login (302).
        self.assertIn(resp.status_code, (302, 403))
        self.work.refresh_from_db()
        self.assertEqual(self.work.title, "Stale")

    def test_anonymous_forbidden(self):
        resp = self.client.post(self._url(), content_type="application/json")
        self.assertIn(resp.status_code, (302, 403))

    def test_get_rejected(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 405)

    def test_no_doi_returns_400(self):
        no_doi = Work.objects.create(title="No DOI", doi=None, status="h")
        self.client.force_login(self.staff)
        resp = self.client.post(f"/work/{no_doi.id}/reharvest/", content_type="application/json")
        self.assertEqual(resp.status_code, 400)
