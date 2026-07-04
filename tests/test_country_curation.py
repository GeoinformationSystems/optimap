# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Staff country-curation section on /countries (issue #261)."""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import GeometryCollection, MultiPolygon, Point, Polygon
from django.core.cache import caches
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.text import slugify

from works.models import SENTINEL_COUNTRY_ISO, Country, Work
from works.tasks import backfill_work_countries
from works.utils.statistics import calculate_statistics


def _box(minx, miny, maxx, maxy):
    return MultiPolygon(Polygon(((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny))))


class _Base(TestCase):
    def setUp(self):
        caches["memory"].clear()
        User = get_user_model()
        self.staff = User.objects.create_user(username="staff", email="staff@example.org", password="x", is_staff=True)
        self.user = User.objects.create_user(username="joe", email="joe@example.org", password="x")
        self.de = Country.objects.create(name="Germany", iso_code="DE", geom=_box(5, 47, 10, 55))
        self.pl = Country.objects.create(name="Poland", iso_code="PL", geom=_box(10, 47, 20, 55))
        # The sentinel row is created by migration 0032_country_sentinel. A
        # TransactionTestCase elsewhere in the suite (e.g.
        # tests.test_ogcapi.OgcApiEndpointTest) truncates this table via
        # `flush` without re-seeding it, so re-create it here to avoid
        # depending on suite run order.
        name = "No country / not applicable"
        self.sentinel, _ = Country.objects.get_or_create(
            iso_code=SENTINEL_COUNTRY_ISO,
            defaults={"name": name, "slug": slugify(name), "geom": MultiPolygon(srid=4326)},
        )

    def _unmatched_work(self, **kw):
        # GEOCODE_WORKS_ON_SAVE defaults off in tests, so no auto-linking.
        return Work.objects.create(status="p", title="w", geometry=GeometryCollection(Point(-30, 0)), **kw)


class SentinelHiddenTests(_Base):
    def test_country_codes_excludes_sentinel(self):
        work = self._unmatched_work()
        work.countries.set([self.de, self.sentinel])
        self.assertEqual(work.country_codes, ["DE"])
        self.assertEqual([c.iso_code for c in work.display_countries], ["DE"])
        self.assertTrue(work.country_match_excluded)

    def test_country_api_excludes_sentinel(self):
        resp = self.client.get("/api/v1/countries/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(SENTINEL_COUNTRY_ISO, resp.content.decode())

    def test_statistics_by_country_excludes_sentinel(self):
        work = Work.objects.create(status="p", title="x", geometry=GeometryCollection(Point(7, 51)))
        work.countries.set([self.sentinel])
        codes = {row["name"] for row in calculate_statistics()["by_country"]}
        self.assertNotIn(SENTINEL_COUNTRY_ISO, codes)

    def test_countries_overview_hides_sentinel(self):
        resp = self.client.get(reverse("optimap:countries"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "No country / not applicable")


class GatingTests(_Base):
    def test_curation_section_staff_only(self):
        url = reverse("optimap:countries")
        self.assertNotContains(self.client.get(url), "Curation: works without a country")
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(url), "Curation: works without a country")
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(url), "Curation: works without a country")

    def test_endpoints_reject_non_staff(self):
        work = self._unmatched_work()
        set_url = reverse("optimap:set-work-country", args=[work.id])
        backfill_url = reverse("optimap:trigger-country-backfill")
        for url in (set_url, backfill_url):
            self.assertNotEqual(self.client.post(url).status_code, 200)  # anonymous → redirect
        self.client.force_login(self.user)
        for url in (set_url, backfill_url):
            self.assertNotEqual(self.client.post(url).status_code, 200)


class AssignAndExcludeTests(_Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)
        self.work = self._unmatched_work()
        self.url = reverse("optimap:set-work-country", args=[self.work.id])

    def _post(self, payload):
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json")

    def test_assign_country(self):
        resp = self._post({"iso_code": "DE"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([c.iso_code for c in self.work.countries.all()], ["DE"])
        self.work.refresh_from_db()
        block = self.work.provenance["countries"]
        self.assertEqual(block["source"], "manual")
        self.assertEqual(block["method"], "curator_assigned")
        self.assertEqual(block["iso_codes"], ["DE"])
        self.assertEqual(block["decided_by"], self.staff.id)
        events = self.work.provenance["events"]
        self.assertEqual(events[-1]["type"], "country_curation")
        self.assertEqual(events[-1]["decision"], "assigned")

    def test_assign_multiple_countries(self):
        resp = self._post({"iso_codes": ["DE", "PL"]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sorted(c.iso_code for c in self.work.countries.all()), ["DE", "PL"])
        self.work.refresh_from_db()
        block = self.work.provenance["countries"]
        self.assertEqual(block["source"], "manual")
        self.assertEqual(block["method"], "curator_assigned")
        self.assertEqual(block["iso_codes"], ["DE", "PL"])

    def test_assign_codes_replace_previous_set(self):
        # iso_codes replaces (set), not adds: a second call overwrites the first.
        self._post({"iso_codes": ["DE", "PL"]})
        self._post({"iso_codes": ["DE"]})
        self.assertEqual([c.iso_code for c in self.work.countries.all()], ["DE"])

    def test_assign_empty_list_rejected(self):
        self.assertEqual(self._post({"iso_codes": []}).status_code, 400)

    def test_assign_list_with_unknown_code_rejected(self):
        resp = self._post({"iso_codes": ["DE", "XX"]})
        self.assertEqual(resp.status_code, 400)
        # Nothing assigned when any code is invalid.
        self.assertEqual(list(self.work.countries.all()), [])

    def test_assign_unknown_code_rejected(self):
        self.assertEqual(self._post({"iso_code": "XX"}).status_code, 400)

    def test_cannot_assign_sentinel_directly(self):
        self.assertEqual(self._post({"iso_code": SENTINEL_COUNTRY_ISO}).status_code, 400)

    def test_exclude_assigns_sentinel(self):
        resp = self._post({"exclude": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([c.iso_code for c in self.work.countries.all()], [SENTINEL_COUNTRY_ISO])
        self.work.refresh_from_db()
        self.assertEqual(self.work.provenance["countries"]["method"], "curator_excluded")
        self.assertEqual(self.work.provenance["countries"]["iso_codes"], [])

    def test_excluded_work_skipped_by_backfill(self):
        # A work over Germany that a curator excluded must NOT be re-matched.
        work = Work.objects.create(status="p", title="ex", geometry=GeometryCollection(Point(7, 51)))
        self.client.post(
            reverse("optimap:set-work-country", args=[work.id]),
            data=json.dumps({"exclude": True}),
            content_type="application/json",
        )
        backfill_work_countries()
        self.assertEqual([c.iso_code for c in work.countries.all()], [SENTINEL_COUNTRY_ISO])

    def test_assigned_work_drops_from_unmatched_list(self):
        self._post({"iso_code": "DE"})
        resp = self.client.get(reverse("optimap:countries"))
        self.assertEqual(resp.context["page_obj"].paginator.count, 0)


class BackfillButtonTests(_Base):
    def test_returns_task_id(self):
        self.client.force_login(self.staff)
        with patch("works.views_indexed.async_task", return_value="deadbeef") as mock:
            resp = self.client.post(reverse("optimap:trigger-country-backfill"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["task_id"], "deadbeef")
        self.assertTrue(data["task_name"])
        mock.assert_called_once_with("works.tasks.backfill_work_countries", trigger_source="manual")


@override_settings(GEOCODE_WORKS_ON_SAVE=True)
class SignalInvalidationTests(_Base):
    def test_manual_decision_sticky_on_unrelated_save(self):
        work = Work.objects.create(status="p", title="m", geometry=GeometryCollection(Point(7, 51)))
        # Curator excludes it.
        self.client.force_login(self.staff)
        self.client.post(
            reverse("optimap:set-work-country", args=[work.id]),
            data=json.dumps({"exclude": True}),
            content_type="application/json",
        )
        # Unrelated save (title change, same geometry) must NOT re-match.
        work.refresh_from_db()
        work.title = "renamed"
        work.save()
        self.assertEqual([c.iso_code for c in work.countries.all()], [SENTINEL_COUNTRY_ISO])

    def test_geometry_change_voids_manual_decision(self):
        work = Work.objects.create(status="p", title="g", geometry=GeometryCollection(Point(-30, 0)))
        self.client.force_login(self.staff)
        # Manually assign DE while geometry is in the ocean.
        self.client.post(
            reverse("optimap:set-work-country", args=[work.id]),
            data=json.dumps({"iso_code": "DE"}),
            content_type="application/json",
        )
        # Now move the geometry into Germany's box but elsewhere — the automated
        # join re-runs and keeps DE (still in DE), proving it re-evaluated.
        work.refresh_from_db()
        work.geometry = GeometryCollection(Point(8, 52))
        work.save()
        work.refresh_from_db()
        self.assertEqual([c.iso_code for c in work.countries.all()], ["DE"])
        # Provenance reverted to an automated join (no longer manual).
        self.assertEqual(work.provenance["countries"]["source"], "natural_earth")

    def test_save_with_invalid_stored_geometry_does_not_crash(self):
        # Defense-in-depth for the track_work_geometry_change signal: a legacy
        # record whose *stored* geometry is invalid (written before save()-time
        # repair existed) makes GEOS refuse the equality predicate. Saving such a
        # record must not raise GEOSException. We bypass Work.save() with a raw
        # UPDATE to plant the invalid geometry, mirroring a pre-existing row.
        bowtie = GeometryCollection(Polygon(((0, 0), (1, 1), (1, 0), (0, 1), (0, 0))))
        work = Work.objects.create(status="p", title="bowtie", geometry=GeometryCollection(Point(-30, 0)))
        Work.objects.filter(pk=work.pk).update(geometry=bowtie)
        work = Work.objects.get(pk=work.pk)
        work.title = "bowtie renamed"
        work.save()  # must not raise; save() also repairs the geometry
        work.refresh_from_db()
        self.assertEqual(work.title, "bowtie renamed")
        self.assertTrue(work.geometry.valid)

    def test_geometry_change_to_ocean_clears_manual_block(self):
        work = Work.objects.create(status="p", title="o", geometry=GeometryCollection(Point(7, 51)))
        self.client.force_login(self.staff)
        self.client.post(
            reverse("optimap:set-work-country", args=[work.id]),
            data=json.dumps({"exclude": True}),
            content_type="application/json",
        )
        work.refresh_from_db()
        work.geometry = GeometryCollection(Point(-30, 0))  # open ocean → no match
        work.save()
        work.refresh_from_db()
        self.assertEqual(list(work.countries.all()), [])
        self.assertIsNone(work.provenance.get("countries"))
