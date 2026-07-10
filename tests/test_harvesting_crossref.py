# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tests for the Crossref-prefix harvester (Copernicus fallback).

Covers:
- the pure helpers (`_strip_jats`, `_build_crossref_filter`,
  `_crossref_item_to_work_kwargs`),
- the publisher-side abstract fetch (`fetch_copernicus_abstract`),
- end-to-end harvest_crossref_prefix with `responses` mocking the Crossref
  API and the publisher landing pages.
"""

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

from datetime import date
from unittest.mock import patch

import responses
from django.test import TestCase

from works.models import HarvestingEvent, Source, Work
from works.tasks import (
    _build_crossref_filter,
    _crossref_item_to_work_kwargs,
    _strip_jats,
    fetch_copernicus_abstract,
    harvest_crossref_book_list,
    harvest_crossref_prefix,
)

SAMPLE_LANDING_HTML = """
<html><head>
  <meta name="citation_abstract" content="<p>Abstract. From the meta tag.</p>" />
</head><body>
  <div class="abstract">
    <strong>Abstract.</strong>
    The growing trend toward urbanisation and the increasingly frequent
    occurrence of extreme weather events emphasise the need for further
    monitoring. <em>Highlighted</em> follow-up sentence.
  </div>
</body></html>
"""

SAMPLE_LANDING_NO_ABSTRACT_DIV = """
<html><head>
  <meta name="citation_abstract" content="From the meta tag, fallback only." />
</head><body><p>No abstract div here.</p></body></html>
"""


class StripJatsTests(TestCase):
    def test_returns_none_for_falsy(self):
        self.assertIsNone(_strip_jats(""))
        self.assertIsNone(_strip_jats(None))

    def test_strips_jats_paragraph_wrapper(self):
        out = _strip_jats("<jats:p>Hello world.</jats:p>")
        self.assertEqual(out, "Hello world.")

    def test_strips_inline_markup(self):
        out = _strip_jats(
            "<jats:p>Hello <jats:italic>world</jats:italic> with <jats:sub>2</jats:sub> subscripts.</jats:p>"
        )
        self.assertEqual(out, "Hello world with 2 subscripts.")


class BuildCrossrefFilterTests(TestCase):
    def test_only_prefix(self):
        self.assertEqual(_build_crossref_filter("10.5194"), "prefix:10.5194")

    def test_with_source_titles(self):
        out = _build_crossref_filter(
            "10.5194",
            source_titles=["Earth System Science Data", "AGILE GIScience Series"],
        )
        self.assertIn("prefix:10.5194", out)
        self.assertIn("container-title:Earth System Science Data", out)
        self.assertIn("container-title:AGILE GIScience Series", out)

    def test_with_since(self):
        out = _build_crossref_filter("10.5194", since="2026-01-01")
        self.assertIn("from-update-date:2026-01-01", out)

    def test_no_prefix_uses_extra_filters_only(self):
        # ESSOAr: no single prefix; base query is member+type via extra_filters.
        out = _build_crossref_filter(
            None,
            extra_filters=["member:311", "type:posted-content"],
            since="2026-01-01",
        )
        self.assertNotIn("prefix:", out)
        self.assertIn("member:311", out)
        self.assertIn("type:posted-content", out)
        self.assertIn("from-update-date:2026-01-01", out)

    def test_with_issn(self):
        # Journals with commas in their titles (e.g. Copernicus GI) use ISSN
        # because Crossref treats commas in filter= as clause separators.
        out = _build_crossref_filter("10.5194", issn="2193-0864")
        self.assertIn("prefix:10.5194", out)
        self.assertIn("issn:2193-0864", out)

    def test_issn_omitted_when_not_provided(self):
        out = _build_crossref_filter("10.5194")
        self.assertNotIn("issn:", out)


class CrossrefItemConversionTests(TestCase):
    def setUp(self):
        # Keep these offline + fast: OpenAlex enrichment is exercised separately.
        patcher = patch("works.harvesting.crossref.build_openalex_fields", return_value=({}, {}))
        self.mock_openalex = patcher.start()
        self.addCleanup(patcher.stop)
        self.source = Source.objects.create(
            name="Crossref Test",
            url_field="https://api.crossref.org/works",
            harvest_interval_minutes=60,
        )
        self.event = HarvestingEvent.objects.create(source=self.source, status="in_progress")

    def _item(self, **overrides):
        item = {
            "DOI": "10.5194/essd-14-4681-2022",
            "URL": "https://doi.org/10.5194/essd-14-4681-2022",
            "title": ["Sample Article"],
            "abstract": "<jats:p>JATS-rendered fallback abstract.</jats:p>",
            "published": {"date-parts": [[2022, 12, 1]]},
        }
        item.update(overrides)
        return item

    def test_returns_none_without_doi(self):
        out = _crossref_item_to_work_kwargs(
            {"title": ["x"]},
            self.source,
            self.event,
            fetch_abstract_from_publisher=False,
            abstract_session=None,
        )
        self.assertIsNone(out)

    def test_uses_crossref_abstract_when_publisher_disabled(self):
        out = _crossref_item_to_work_kwargs(
            self._item(),
            self.source,
            self.event,
            fetch_abstract_from_publisher=False,
            abstract_session=None,
        )
        self.assertEqual(out["doi"], "10.5194/essd-14-4681-2022")
        self.assertEqual(out["abstract"], "JATS-rendered fallback abstract.")
        self.assertEqual(out["publicationDate"], date(2022, 12, 1))
        self.assertEqual(out["status"], "h")

    @responses.activate
    def test_prefers_publisher_abstract_when_available(self):
        responses.add(
            responses.GET,
            "https://doi.org/10.5194/essd-14-4681-2022",
            body=SAMPLE_LANDING_HTML,
            content_type="text/html",
            status=200,
        )
        out = _crossref_item_to_work_kwargs(
            self._item(),
            self.source,
            self.event,
            fetch_abstract_from_publisher=True,
            abstract_session=None,
        )
        self.assertIn("urbanisation", out["abstract"].lower())
        # The literal "Abstract" lead should have been stripped.
        self.assertFalse(
            out["abstract"].lower().startswith("abstract"),
            f"Stale Abstract prefix: {out['abstract'][:40]!r}",
        )

    @responses.activate
    def test_falls_back_to_jats_when_landing_page_404s(self):
        responses.add(
            responses.GET,
            "https://doi.org/10.5194/essd-14-4681-2022",
            status=404,
        )
        out = _crossref_item_to_work_kwargs(
            self._item(),
            self.source,
            self.event,
            fetch_abstract_from_publisher=True,
            abstract_session=None,
        )
        self.assertEqual(out["abstract"], "JATS-rendered fallback abstract.")


class FetchCopernicusAbstractTests(TestCase):
    @responses.activate
    def test_uses_div_abstract_when_present(self):
        responses.add(
            responses.GET,
            "https://essd.copernicus.org/articles/14/4681/2022/",
            body=SAMPLE_LANDING_HTML,
            content_type="text/html",
            status=200,
        )
        out = fetch_copernicus_abstract("https://essd.copernicus.org/articles/14/4681/2022/")
        self.assertIn("urbanisation", out.lower())

    @responses.activate
    def test_falls_back_to_meta_when_div_absent(self):
        responses.add(
            responses.GET,
            "https://example.copernicus.org/article/",
            body=SAMPLE_LANDING_NO_ABSTRACT_DIV,
            content_type="text/html",
            status=200,
        )
        out = fetch_copernicus_abstract("https://example.copernicus.org/article/")
        self.assertEqual(out, "From the meta tag, fallback only.")

    @responses.activate
    def test_returns_none_on_http_error(self):
        responses.add(
            responses.GET,
            "https://example.copernicus.org/down/",
            status=503,
        )
        self.assertIsNone(
            fetch_copernicus_abstract(
                "https://example.copernicus.org/down/",
            )
        )

    def test_returns_none_for_empty_url(self):
        self.assertIsNone(fetch_copernicus_abstract(""))
        self.assertIsNone(fetch_copernicus_abstract(None))

    def test_returns_none_when_fetch_exceeds_wall_clock_deadline(self):
        # Regression: a server that trickles a response slowly enough to keep
        # resetting requests' own per-read timeout can hang a call far beyond
        # its declared timeout= — fetch_with_deadline bounds that, and this
        # function must treat the resulting TimeoutError like any other
        # fetch failure (degrade to None, not propagate/crash the harvest).
        with patch("works.harvesting.crossref.fetch_with_deadline", side_effect=TimeoutError("exceeded deadline")):
            self.assertIsNone(fetch_copernicus_abstract("https://essd.copernicus.org/articles/14/4681/2022/"))


class HarvestCrossrefPrefixEndToEndTests(TestCase):
    def setUp(self):
        from works.models import Collection

        patcher = patch("works.harvesting.crossref.build_openalex_fields", return_value=({}, {}))
        self.mock_openalex = patcher.start()
        self.addCleanup(patcher.stop)

        collection, _ = Collection.objects.get_or_create(
            identifier="copernicus-publications",
            defaults={"name": "Copernicus Publications", "is_published": True},
        )
        self.source = Source.objects.create(
            name="Copernicus Crossref",
            url_field="https://api.crossref.org/works?filter=prefix:10.5194",
            source_type="crossref-prefix",
            collection=collection,
            harvest_interval_minutes=60 * 24 * 7,
            publisher_name="Copernicus Publications",
            is_oa=True,
            default_work_type="article",
        )

    def _crossref_response(self, items, next_cursor=None):
        return {
            "status": "ok",
            "message": {
                "total-results": len(items),
                "items": items,
                "next-cursor": next_cursor or "",
            },
        }

    @responses.activate
    def test_end_to_end_creates_works_with_publisher_abstracts(self):
        items = [
            {
                "DOI": "10.5194/essd-14-4681-2022",
                "URL": "https://doi.org/10.5194/essd-14-4681-2022",
                "title": ["Sample article 1"],
                "abstract": "<jats:p>fallback 1</jats:p>",
                "published-online": {"date-parts": [[2022, 12, 1]]},
            },
            {
                "DOI": "10.5194/essd-15-1-2023",
                "URL": "https://doi.org/10.5194/essd-15-1-2023",
                "title": ["Sample article 2"],
                "abstract": "<jats:p>fallback 2</jats:p>",
                "published-online": {"date-parts": [[2023, 1, 1]]},
            },
        ]
        responses.add(
            responses.GET,
            "https://api.crossref.org/works",
            json=self._crossref_response(items),
            status=200,
        )
        # Both DOI redirects return the same canonical landing page (close
        # enough to reality — each journal's landing page has the same
        # abstract structure).
        for item in items:
            responses.add(
                responses.GET,
                item["URL"],
                body=SAMPLE_LANDING_HTML,
                content_type="text/html",
                status=200,
            )

        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=True)

        works = Work.objects.filter(source=self.source).order_by("doi")
        self.assertEqual(works.count(), 2)
        for w in works:
            self.assertIn("urbanisation", w.abstract.lower())
            self.assertEqual(w.status, "h")

        event = HarvestingEvent.objects.filter(source=self.source).latest("started_at")
        self.assertEqual(event.status, "completed")

    @responses.activate
    def test_skips_existing_dois(self):
        # Pre-create one Work by DOI; the harvester should skip it.
        Work.objects.create(
            title="Pre-existing",
            doi="10.5194/already-here",
            source=self.source,
            status="p",
        )
        items = [
            {
                "DOI": "10.5194/already-here",
                "URL": "https://doi.org/10.5194/already-here",
                "title": ["dup"],
                "abstract": "<jats:p>x</jats:p>",
                "published": {"date-parts": [[2024, 1, 1]]},
            },
            {
                "DOI": "10.5194/new",
                "URL": "https://doi.org/10.5194/new",
                "title": ["new"],
                "abstract": "<jats:p>y</jats:p>",
                "published": {"date-parts": [[2024, 2, 1]]},
            },
        ]
        responses.add(
            responses.GET,
            "https://api.crossref.org/works",
            json=self._crossref_response(items),
            status=200,
        )
        # Only the "new" landing page will be hit (publisher fetch off).
        harvest_crossref_prefix(
            self.source.id,
            fetch_abstract_from_publisher=False,
        )
        # 1 pre-existing + 1 new = 2 total
        self.assertEqual(Work.objects.filter(source=self.source).count(), 2)
        self.assertTrue(Work.objects.filter(doi="10.5194/new").exists())

    @responses.activate
    def test_max_records_caps_processing(self):
        items = [
            {
                "DOI": f"10.5194/cap-{i}",
                "URL": f"https://doi.org/10.5194/cap-{i}",
                "title": [f"cap {i}"],
                "abstract": f"<jats:p>cap {i}</jats:p>",
                "published": {"date-parts": [[2024, 1, 1]]},
            }
            for i in range(5)
        ]
        responses.add(
            responses.GET,
            "https://api.crossref.org/works",
            json=self._crossref_response(items),
            status=200,
        )
        harvest_crossref_prefix(
            self.source.id,
            max_records=2,
            fetch_abstract_from_publisher=False,
        )
        self.assertEqual(Work.objects.filter(source=self.source).count(), 2)

    @responses.activate
    def test_journal_title_filter_passed_to_crossref(self):
        responses.add(
            responses.GET,
            "https://api.crossref.org/works",
            json=self._crossref_response([]),
            status=200,
        )
        harvest_crossref_prefix(
            self.source.id,
            source_titles=["Earth System Science Data"],
            fetch_abstract_from_publisher=False,
        )
        # Only one request was made; check its query string.
        self.assertEqual(len(responses.calls), 1)
        called = responses.calls[0].request.url
        self.assertIn("container-title%3AEarth+System+Science+Data", called.replace(":", "%3A"))

    def _page(self, items, total_results, next_cursor):
        """Build a Crossref message page with an explicit total-results.

        `_crossref_response` derives total-results from len(items), which is
        wrong for the transient-empty-page scenarios below — the empty page
        must still report the full total so the harvester knows to keep going.
        """
        return {
            "status": "ok",
            "message": {
                "total-results": total_results,
                "items": items,
                "next-cursor": next_cursor,
            },
        }

    @responses.activate
    @patch("works.harvesting.crossref.time.sleep", lambda *_: None)
    def test_recovers_from_transient_empty_page(self):
        # Crossref occasionally returns an empty `items` page mid-walk under
        # load. The harvester must retry the same cursor instead of treating
        # it as end-of-results and silently truncating (issue: 8000/8387 bug).
        item_a = {
            "DOI": "10.1038/sdata-a",
            "URL": "https://doi.org/10.1038/sdata-a",
            "title": ["A"],
            "abstract": "<jats:p>a</jats:p>",
            "published": {"date-parts": [[2024, 1, 1]]},
        }
        item_b = {
            "DOI": "10.1038/sdata-b",
            "URL": "https://doi.org/10.1038/sdata-b",
            "title": ["B"],
            "abstract": "<jats:p>b</jats:p>",
            "published": {"date-parts": [[2024, 2, 1]]},
        }
        # page 1: A + next-cursor; page 2: EMPTY (transient); retry: B + end.
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._page([item_a], 2, "c1"), status=200)
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._page([], 2, "c1"), status=200)
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._page([item_b], 2, ""), status=200)

        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False)

        # Both works must be saved — the transient empty page must not truncate.
        self.assertEqual(Work.objects.filter(source=self.source).count(), 2)
        self.assertTrue(Work.objects.filter(doi="10.1038/sdata-b").exists())

    @responses.activate
    @patch("works.harvesting.crossref.time.sleep", lambda *_: None)
    def test_warns_when_crossref_truncates_below_total(self):
        # If Crossref keeps returning empty pages past the retry budget, the
        # harvest gives up but must record a visible warning rather than report
        # a clean completion (the original silent-truncation behaviour).
        item_a = {
            "DOI": "10.1038/sdata-a",
            "URL": "https://doi.org/10.1038/sdata-a",
            "title": ["A"],
            "abstract": "<jats:p>a</jats:p>",
            "published": {"date-parts": [[2024, 1, 1]]},
        }
        responses.add(
            responses.GET, "https://api.crossref.org/works", json=self._page([item_a], 100, "c1"), status=200
        )
        # Every subsequent request returns an empty page; total-results=100 so
        # the harvester knows it is short. responses reuses the last match.
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._page([], 100, "c1"), status=200)

        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False)

        event = HarvestingEvent.objects.filter(source=self.source).latest("started_at")
        self.assertEqual(event.status, "completed")
        self.assertIn("stopped early", (event.log_text or "").lower())


class HarvestCrossrefDoiContainsTests(TestCase):
    """ESS Open Archive spans two DOI eras (10.1002/essoar.* + 10.22541/essoar.*)
    that share Wiley member 311 / posted-content with Authorea. The source uses a
    raw ``crossref_filter`` (member+type) base query narrowed by ``doi_contains``."""

    def setUp(self):
        from works.models import Collection

        patcher = patch("works.harvesting.crossref.build_openalex_fields", return_value=({}, {}))
        self.mock_openalex = patcher.start()
        self.addCleanup(patcher.stop)

        self.collection, _ = Collection.objects.get_or_create(
            identifier="ess-open-archive",
            defaults={"name": "ESS Open Archive", "is_published": True},
        )
        self.source = Source.objects.create(
            name="ESS Open Archive",
            url_field="https://api.crossref.org/works?filter=member:311,type:posted-content",
            source_type="crossref-prefix",
            collection=self.collection,
            crossref_filter="member:311,type:posted-content",
            doi_contains="essoar",
            harvest_interval_minutes=60 * 24,
            is_oa=True,
            is_preprint=True,
            default_work_type="preprint",
        )

    def _item(self, doi):
        return {
            "DOI": doi,
            "URL": f"https://doi.org/{doi}",
            "title": [doi],
            "abstract": "<jats:p>abstract</jats:p>",
            "published": {"date-parts": [[2024, 1, 1]]},
        }

    def _crossref_response(self, items):
        return {
            "status": "ok",
            "message": {"total-results": len(items), "items": items, "next-cursor": ""},
        }

    @responses.activate
    def test_keeps_only_essoar_dois_across_both_eras(self):
        items = [
            self._item("10.1002/essoar.10503356.1"),  # legacy era — keep
            self._item("10.22541/au.222/v1"),  # Authorea — skip
            self._item("10.22541/essoar.333/v1"),  # current era — keep
            self._item("10.22541/authorea.444/v1"),  # Authorea variant — skip
            self._item("10.1002/oarr.555/v1"),  # other Wiley posted-content — skip
        ]
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response(items), status=200)

        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False)

        dois = set(Work.objects.filter(source=self.source).values_list("doi", flat=True))
        self.assertEqual(dois, {"10.1002/essoar.10503356.1", "10.22541/essoar.333/v1"})
        self.assertEqual(Work.objects.filter(source=self.source, collections=self.collection).count(), 2)

    @responses.activate
    def test_full_backfill_windows_by_created_date(self):
        # A full backfill of a shared-member source is partitioned into yearly
        # deposit-date windows, so it issues many calls each carrying a
        # from-created-date/until-created-date pair rather than one giant walk.
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response([]), status=200)
        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False, full=True)

        # Ignore the rows=0 works-count probe; the paging walk uses a cursor.
        walk_urls = [call.request.url for call in responses.calls if "rows=0" not in call.request.url]
        self.assertGreater(len(walk_urls), 1)
        self.assertTrue(all("from-created-date" in url for url in walk_urls))
        self.assertTrue(any("until-created-date" in url for url in walk_urls))

    @responses.activate
    def test_bounded_smoke_test_is_not_windowed(self):
        # A bounded max_records run (smoke test) stays a single cursor walk even
        # on a first run, rather than fanning out into per-year windows.
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response([]), status=200)
        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False, max_records=50)

        walk_urls = [call.request.url for call in responses.calls if "rows=0" not in call.request.url]
        self.assertEqual(len(walk_urls), 1)
        self.assertNotIn("from-created-date", walk_urls[0])

    @responses.activate
    def test_harvest_collapses_essoar_versions_to_latest(self):
        items = [
            self._item("10.22541/essoar.123.456/v1"),
            self._item("10.22541/essoar.123.456/v2"),
        ]
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response(items), status=200)

        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False)

        live = Work.objects.filter(source=self.source).exclude(status="r")
        self.assertEqual([w.doi for w in live], ["10.22541/essoar.123.456/v2"])
        tomb = Work.objects.get(doi="10.22541/essoar.123.456/v1")
        self.assertEqual(tomb.status, "r")

    @responses.activate
    def test_query_uses_member_type_filter_not_prefix(self):
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response([]), status=200)
        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False)
        url = responses.calls[0].request.url
        self.assertIn("member%3A311", url.replace(":", "%3A"))
        self.assertIn("type%3Aposted-content", url.replace(":", "%3A"))
        self.assertNotIn("prefix%3A", url.replace(":", "%3A"))

    @responses.activate
    def test_first_run_has_no_from_update_date(self):
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response([]), status=200)
        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False)
        self.assertNotIn("from-update-date", responses.calls[0].request.url)

    @responses.activate
    def test_second_run_is_incremental(self):
        # A prior completed event makes the next run incremental.
        from django.utils import timezone

        prior = HarvestingEvent.objects.create(source=self.source, status="completed")
        prior.completed_at = timezone.now()
        prior.save(update_fields=["completed_at"])

        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response([]), status=200)
        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False)
        self.assertIn("from-update-date", responses.calls[0].request.url)

    @responses.activate
    def test_capped_prior_event_does_not_become_incremental_watermark(self):
        # Regression: a max_records-capped run (e.g. a 20-record smoke test)
        # must not advance the incremental "since" window — records that
        # existed upstream before that run but were excluded purely by the
        # cap would otherwise be permanently skipped by every later run.
        from django.utils import timezone

        capped = HarvestingEvent.objects.create(source=self.source, status="completed", max_records=20)
        capped.completed_at = timezone.now()
        capped.save(update_fields=["completed_at"])

        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response([]), status=200)
        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False)
        self.assertNotIn("from-update-date", responses.calls[0].request.url)

    @responses.activate
    def test_uncapped_event_before_a_capped_one_is_still_the_watermark(self):
        from datetime import timedelta

        from django.utils import timezone

        full_run = HarvestingEvent.objects.create(source=self.source, status="completed", max_records=None)
        full_run.completed_at = timezone.now() - timedelta(days=10)
        full_run.save(update_fields=["completed_at"])
        capped = HarvestingEvent.objects.create(source=self.source, status="completed", max_records=20)
        capped.completed_at = timezone.now()
        capped.save(update_fields=["completed_at"])

        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response([]), status=200)
        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False)
        expected = (full_run.completed_at.date() - timedelta(days=2)).isoformat()
        self.assertIn(f"from-update-date%3A{expected}", responses.calls[0].request.url.replace(":", "%3A"))

    @responses.activate
    def test_full_forces_backfill_ignoring_prior_event(self):
        # Even with a prior completed event, full=True drops the incremental window.
        from django.utils import timezone

        prior = HarvestingEvent.objects.create(source=self.source, status="completed")
        prior.completed_at = timezone.now()
        prior.save(update_fields=["completed_at"])

        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response([]), status=200)
        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False, full=True)
        self.assertNotIn("from-update-date", responses.calls[0].request.url)

    @responses.activate
    def test_explicit_since_overrides_derived_window(self):
        from django.utils import timezone

        prior = HarvestingEvent.objects.create(source=self.source, status="completed")
        prior.completed_at = timezone.now()
        prior.save(update_fields=["completed_at"])

        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response([]), status=200)
        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False, since="2020-01-01")
        self.assertIn("from-update-date%3A2020-01-01", responses.calls[0].request.url.replace(":", "%3A"))


class CrossrefOpenAlexEnrichmentTests(TestCase):
    """The Crossref harvester enriches each work via OpenAlex (DOI match),
    filling topics / openalex_* identity fields while Crossref-supplied values
    (authors, biblio, work type) take precedence."""

    def setUp(self):
        from works.models import Collection

        self.collection, _ = Collection.objects.get_or_create(
            identifier="copernicus-publications",
            defaults={"name": "Copernicus Publications", "is_published": True},
        )
        self.source = Source.objects.create(
            name="Copernicus Crossref",
            url_field="https://api.crossref.org/works?filter=prefix:10.5194",
            source_type="crossref-prefix",
            collection=self.collection,
            doi_prefix="10.5194",
            harvest_interval_minutes=0,
            default_work_type="article",
        )

    def _crossref_response(self, items):
        return {"status": "ok", "message": {"total-results": len(items), "items": items, "next-cursor": ""}}

    @responses.activate
    @patch("works.harvesting.crossref.build_openalex_fields")
    def test_openalex_fields_applied_crossref_wins(self, mock_build):
        # OpenAlex offers topics + identity + biblio + a different type/authors.
        mock_build.return_value = (
            {
                "topics": ["Hydrology"],
                "keywords": ["flood"],
                "openalex_id": "W123",
                "type": "report",  # must NOT override source default_work_type
                "authors": ["OpenAlex Author"],  # must NOT override Crossref authors
                "volume": "99",  # must NOT override Crossref volume
            },
            {
                "topics": "openalex",
                "keywords": "openalex",
                "type": "openalex",
                "authors": "openalex",
                "volume": "openalex",
            },
        )
        item = {
            "DOI": "10.5194/x-1-2024",
            "URL": "https://doi.org/10.5194/x-1-2024",
            "title": ["Enriched article"],
            "abstract": "<jats:p>a</jats:p>",
            "published": {"date-parts": [[2024, 1, 1]]},
            "author": [{"given": "Jane", "family": "Doe"}],
            "volume": "12",
        }
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response([item]))

        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False)

        mock_build.assert_called_once()
        w = Work.objects.get(doi="10.5194/x-1-2024")
        # OpenAlex fills topics/keywords/identity that Crossref lacks.
        self.assertEqual(w.topics, ["Hydrology"])
        self.assertEqual(w.keywords, ["flood"])
        self.assertEqual(w.openalex_id, "W123")
        # Crossref wins for authors, biblio, and work type.
        self.assertEqual(w.authors, ["Jane Doe"])
        self.assertEqual(w.volume, "12")
        self.assertEqual(w.type, "article")
        # Provenance reflects the precedence.
        ms = w.provenance["metadata_sources"]
        self.assertEqual(ms.get("authors"), "crossref")
        self.assertEqual(ms.get("biblio"), "crossref")
        self.assertEqual(ms.get("topics"), "openalex")

    @responses.activate
    @patch("works.harvesting.crossref.build_openalex_fields", side_effect=RuntimeError("OpenAlex down"))
    def test_openalex_failure_does_not_break_harvest(self, _mock):
        item = {
            "DOI": "10.5194/y-1-2024",
            "URL": "https://doi.org/10.5194/y-1-2024",
            "title": ["Resilient article"],
            "abstract": "<jats:p>a</jats:p>",
            "published": {"date-parts": [[2024, 1, 1]]},
        }
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response([item]))

        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False)

        self.assertTrue(Work.objects.filter(doi="10.5194/y-1-2024").exists())
        event = HarvestingEvent.objects.filter(source=self.source).latest("started_at")
        self.assertEqual(event.status, "completed")


class CrossrefOpenAlexAuthorOrcidLinkingTests(TestCase):
    """End-to-end regression test: a Crossref harvest whose OpenAlex match
    carries ORCID-bearing authorships must actually create Author/WorkAuthor
    rows — exercising the *real* build_openalex_fields() (not mocked out, as
    ``CrossrefOpenAlexEnrichmentTests`` does above), since the bug this
    guards against lived inside that function.
    """

    def setUp(self):
        self.source = Source.objects.create(
            name="Copernicus Crossref",
            url_field="https://api.crossref.org/works?filter=prefix:10.5194",
            source_type="crossref-prefix",
            doi_prefix="10.5194",
            harvest_interval_minutes=0,
            default_work_type="article",
        )

    def _crossref_response(self, items):
        return {"status": "ok", "message": {"total-results": len(items), "items": items, "next-cursor": ""}}

    @responses.activate
    @patch("works.harvesting.openalex.get_openalex_matcher")
    def test_orcid_bearing_authorship_gets_linked(self, mock_get_matcher):
        mock_get_matcher.return_value.match_publication.return_value = (
            {
                "openalex_id": "W123",
                "author_records": [
                    {"name": "Jane Doe", "orcid": "0000-0001-2345-6789", "openalex_id": "A1", "rank": 0}
                ],
            },
            None,
        )
        item = {
            "DOI": "10.5194/orcid-1-2024",
            "URL": "https://doi.org/10.5194/orcid-1-2024",
            "title": ["ORCID-linked article"],
            "abstract": "<jats:p>a</jats:p>",
            "published": {"date-parts": [[2024, 1, 1]]},
        }
        responses.add(responses.GET, "https://api.crossref.org/works", json=self._crossref_response([item]))

        harvest_crossref_prefix(self.source.id, fetch_abstract_from_publisher=False)

        w = Work.objects.get(doi="10.5194/orcid-1-2024")
        self.assertTrue(w.author_links.exists())
        linked_author = w.author_links.get()
        self.assertEqual(linked_author.orcid, "0000-0001-2345-6789")


class TryBokPdfExtractionErrorContainmentTests(TestCase):
    """``_try_bok_pdf_extraction`` must catch its own errors rather than
    relying on the caller's "failed to persist" catch — otherwise a PDF
    fetch/parse failure gets misreported as the Work save itself failing,
    when the save (the part that actually matters) already succeeded.
    """

    def setUp(self):
        self.source = Source.objects.create(name="AGILE GISS", url_field="http://example.org/agile")
        self.work = Work.objects.create(
            title="AGILE GISS Paper",
            doi="10.5194/agile-giss-6-2-2025",
            source=self.source,
            status="h",
        )

    def test_pdf_download_failure_does_not_raise(self):
        from works.harvesting.crossref import _try_bok_pdf_extraction

        with patch("works.harvesting.crossref.extract_bok_from_agile_pdf", side_effect=RuntimeError("PDF fetch boom")):
            _try_bok_pdf_extraction(self.work, self.work.doi, session=None)  # must not raise

    def test_pdf_download_failure_logs_bok_specific_message(self):
        from works.harvesting.crossref import _try_bok_pdf_extraction

        with (
            patch("works.harvesting.crossref.extract_bok_from_agile_pdf", side_effect=RuntimeError("PDF fetch boom")),
            self.assertLogs("works.harvesting.crossref", level="WARNING") as cm,
        ):
            _try_bok_pdf_extraction(self.work, self.work.doi, session=None)

        self.assertTrue(any("BoK PDF extraction failed" in msg for msg in cm.output))
        self.assertFalse(any("Failed to persist" in msg for msg in cm.output))


class BuildCrossrefFilterExtraTests(TestCase):
    def test_extra_filters_appended(self):
        out = _build_crossref_filter("10.1007", extra_filters=["isbn:9783030147457"])
        self.assertIn("prefix:10.1007", out)
        self.assertIn("isbn:9783030147457", out)

    def test_extra_filters_combined_with_since(self):
        out = _build_crossref_filter(
            "10.1007",
            since="2020-01-01",
            extra_filters=["isbn:9783030147457"],
        )
        self.assertIn("from-update-date:2020-01-01", out)
        self.assertIn("isbn:9783030147457", out)


class HarvestCrossrefBookListTests(TestCase):
    def setUp(self):
        from works.models import Collection

        patcher = patch("works.harvesting.crossref.build_openalex_fields", return_value=({}, {}))
        self.mock_openalex = patcher.start()
        self.addCleanup(patcher.stop)

        self.collection, _ = Collection.objects.get_or_create(
            identifier="agile-gis",
            defaults={"name": "AGILE GIS", "is_published": True},
        )
        self.source = Source.objects.create(
            name="AGILE: Springer LNCS Proceedings",
            url_field="https://api.crossref.org/works?filter=prefix:10.1007",
            source_type="crossref-prefix",
            collection=self.collection,
            doi_prefix="10.1007",
            harvest_interval_minutes=0,
            publisher_name="Springer",
            is_oa=False,
            default_work_type="proceedings-article",
        )

    def _crossref_response(self, items, next_cursor=None):
        return {
            "status": "ok",
            "message": {
                "total-results": len(items),
                "items": items,
                "next-cursor": next_cursor or "",
            },
        }

    def _chapter(self, doi, title="A chapter"):
        return {
            "DOI": doi,
            "URL": f"https://doi.org/{doi}",
            "title": [title],
            "abstract": "<jats:p>Chapter abstract.</jats:p>",
            "published": {"date-parts": [[2019, 4, 16]]},
        }

    @responses.activate
    def test_creates_works_for_each_isbn(self):
        isbns = ["978-3-030-14745-7", "978-3-319-16787-9"]
        # Return 2 chapters for first ISBN, 1 for second.
        responses.add(
            responses.GET,
            "https://api.crossref.org/works",
            json=self._crossref_response(
                [
                    self._chapter("10.1007/978-3-030-14745-7_1", "Chapter 1a"),
                    self._chapter("10.1007/978-3-030-14745-7_2", "Chapter 1b"),
                ]
            ),
        )
        responses.add(
            responses.GET,
            "https://api.crossref.org/works",
            json=self._crossref_response(
                [
                    self._chapter("10.1007/978-3-319-16787-9_1", "Chapter 2a"),
                ]
            ),
        )
        # DOI landing page fetches raise ConnectionError (unregistered URL),
        # caught gracefully — abstract falls back to JATS.

        harvest_crossref_book_list(
            self.source.id,
            book_isbns=isbns,
        )

        self.assertEqual(Work.objects.filter(source=self.source).count(), 3)
        # All added to collection.
        self.assertEqual(Work.objects.filter(source=self.source, collections=self.collection).count(), 3)
        # Single HarvestingEvent covers all ISBNs.
        self.assertEqual(HarvestingEvent.objects.filter(source=self.source).count(), 1)
        event = HarvestingEvent.objects.get(source=self.source)
        self.assertEqual(event.status, "completed")

    @responses.activate
    def test_isbn_filter_included_in_crossref_request(self):
        responses.add(
            responses.GET,
            "https://api.crossref.org/works",
            json=self._crossref_response([]),
        )
        harvest_crossref_book_list(
            self.source.id,
            book_isbns=["978-3-030-14745-7"],
        )
        # Harvest call has isbn in URL; stats call (rows=0) does not — filter to harvest calls only.
        harvest_calls = [c for c in responses.calls if "isbn" in c.request.url]
        self.assertEqual(len(harvest_calls), 1)
        called_url = harvest_calls[0].request.url
        self.assertIn("isbn", called_url)
        self.assertIn("9783030147457", called_url.replace("-", "").replace("%2D", ""))

    @responses.activate
    def test_max_records_stops_early(self):
        isbns = ["978-3-030-14745-7", "978-3-319-16787-9"]
        responses.add(
            responses.GET,
            "https://api.crossref.org/works",
            json=self._crossref_response(
                [
                    self._chapter("10.1007/978-3-030-14745-7_1"),
                    self._chapter("10.1007/978-3-030-14745-7_2"),
                ]
            ),
        )
        # DOI landing page fetches raise ConnectionError — caught gracefully.

        harvest_crossref_book_list(
            self.source.id,
            book_isbns=isbns,
            max_records=2,
        )
        # Stopped after first ISBN (2 records = max_records); second ISBN not requested.
        # Count only harvest calls (isbn filter); exclude trailing stats request (rows=0).
        api_calls = [c for c in responses.calls if "api.crossref.org" in c.request.url and "isbn" in c.request.url]
        self.assertEqual(len(api_calls), 1)

    @responses.activate
    def test_provenance_records_correct_harvester_name(self):
        responses.add(
            responses.GET,
            "https://api.crossref.org/works",
            json=self._crossref_response([self._chapter("10.1007/978-3-030-14745-7_1")]),
        )
        # DOI landing page fetch raises ConnectionError — caught gracefully.

        harvest_crossref_book_list(
            self.source.id,
            book_isbns=["978-3-030-14745-7"],
        )
        work = Work.objects.get(doi="10.1007/978-3-030-14745-7_1")
        self.assertEqual(work.provenance["harvest"]["harvester"], "harvest_crossref_book_list")
