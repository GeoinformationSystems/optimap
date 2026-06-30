# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""SEO + Zotero / reference-manager compatibility tests (issues #22, #243).

Covers Open Graph / Twitter Card / schema.org JSON-LD / Google Scholar
``citation_*`` tags on the work landing page, ``WebSite`` / ``SearchAction``
JSON-LD on the homepage, ``CollectionPage`` JSON-LD on feed pages, the
preview-image generator + cache invalidation, and the Zotero-targeted
extensions (full ``citation_abstract``, ``citation_publisher``,
``citation_language``, repeated ``citation_keywords``, ``citation_pdf_url``,
journal-citation fields, nested ``PublicationIssue`` / ``PublicationVolume``
JSON-LD, OpenAlex/permalink ``sameAs`` linkbacks, and the COinS Z3988 span).
Avoids the network: the preview renderer is monkey-patched to skip OSM tile
fetches.
"""

from __future__ import annotations

import json
import os
from unittest import mock

import django
from bs4 import BeautifulSoup
from django.test import Client, TestCase, override_settings
from django.urls import reverse

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

from django.contrib.gis.geos import GeometryCollection, GEOSGeometry

from works.models import Source, Work


def _polygon_collection(coords):
    geom = GEOSGeometry(json.dumps({"type": "Polygon", "coordinates": [coords]}), srid=4326)
    return GeometryCollection(geom, srid=4326)


def _make_published_work(**overrides) -> Work:
    src = Source.objects.create(
        name="SEO Test Journal",
        url_field="https://example.test/oai",
        homepage_url="https://example.test/",
        issn_l="1234-5678",
        harvest_interval_minutes=1440,
    )
    sulawesi = [[119.0, -5.7], [125.0, -5.7], [125.0, 1.7], [119.0, 1.7], [119.0, -5.7]]
    defaults = dict(
        title="Pollen, ash, and the Sulawesi caves: a chronology",
        abstract="A short abstract about Sulawesi cave sediments.",
        url="https://example.test/article/1",
        doi="10.1234/test.seo.1",
        source=src,
        status="p",
        geometry=_polygon_collection(sulawesi),
        timeperiod_startdate=[None],
        timeperiod_enddate=["2024-12-31"],
        authors=["Sontag-Gonzalez, Mariana", "Roberts, Richard G."],
        keywords=["Earth Sciences"],
        topics=["Geomagnetism and Paleomagnetism Studies"],
        publicationDate="2025-01-14",
    )
    defaults.update(overrides)
    return Work.objects.create(**defaults)


def _find_jsonld(soup: BeautifulSoup) -> list[dict]:
    blobs = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if tag.string and tag.string.strip():
            try:
                blobs.append(json.loads(tag.string))
            except json.JSONDecodeError:
                pass
    return blobs


def _indonesia():
    from django.contrib.gis.geos import MultiPolygon, Polygon

    from works.models import Country

    return Country.objects.create(
        name="Indonesia",
        iso_code="ID",
        geom=MultiPolygon(Polygon(((118, -3), (122, -3), (122, 1), (118, 1), (118, -3)))),
    )


@override_settings(GEOCODE_WORKS_ON_SAVE=False)
class WorkLandingSEOTests(TestCase):
    """SEO / meta-tag rendering tests. The reverse-geocoding pre_save signal
    is disabled here so the test suite never hits Nominatim — placename is
    exercised by the dedicated tests in test_geocoding.py, and country
    association (Work.countries) is linked directly here to verify the
    *rendering* path."""

    def setUp(self):
        self.client = Client()
        self.work = _make_published_work()
        self.url = reverse("optimap:work-landing", args=[self.work.get_identifier()])

    def test_open_graph_and_twitter_tags_present(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        soup = BeautifulSoup(resp.content, "html.parser")
        for prop in ("og:title", "og:description", "og:url", "og:type", "og:site_name"):
            self.assertIsNotNone(
                soup.find("meta", attrs={"property": prop}),
                f"missing OG tag: {prop}",
            )
        for name in ("twitter:card", "twitter:title", "twitter:description"):
            self.assertIsNotNone(
                soup.find("meta", attrs={"name": name}),
                f"missing Twitter tag: {name}",
            )
        og_type = soup.find("meta", attrs={"property": "og:type"})
        self.assertEqual(og_type["content"], "article")

    def test_og_image_points_at_preview_endpoint(self):
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        og_image = soup.find("meta", attrs={"property": "og:image"})
        self.assertIsNotNone(og_image, "og:image is required when geometry is present")
        preview_url = reverse("optimap:work-preview", args=[self.work.get_identifier()])
        self.assertIn(preview_url, og_image["content"])

    def test_og_image_uses_fallback_for_work_without_geometry(self):
        # Works without geometry use the static branded fallback so social
        # link previews always render with an image (closes #226).
        no_geom = Work.objects.create(
            title="No-extent work",
            abstract="An abstract with no spatial coverage.",
            url="https://example.test/article/2",
            doi="10.1234/test.seo.2",
            source=self.work.source,
            status="p",
            geometry=GeometryCollection(),
            publicationDate="2025-01-14",
        )
        resp = self.client.get(reverse("optimap:work-landing", args=[no_geom.get_identifier()]))
        soup = BeautifulSoup(resp.content, "html.parser")
        og_image = soup.find("meta", attrs={"property": "og:image"})
        self.assertIsNotNone(og_image, "og:image must be present even for geometry-less works")
        self.assertIn("og-fallback.png", og_image["content"])

    def test_canonical_link_present(self):
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        link = soup.find("link", attrs={"rel": "canonical"})
        self.assertIsNotNone(link)
        self.assertIn(self.url, link["href"])

    def test_jsonld_scholarly_article(self):
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        blobs = _find_jsonld(soup)
        article = next(
            (b for b in blobs if b.get("@type") == "ScholarlyArticle"),
            None,
        )
        self.assertIsNotNone(article, "ScholarlyArticle JSON-LD missing")
        self.assertEqual(article["name"], self.work.title)
        self.assertEqual(article.get("identifier"), f"doi:{self.work.doi}")
        self.assertEqual(article["sameAs"], f"https://doi.org/{self.work.doi}")
        self.assertEqual(len(article["author"]), 2)
        # spatialCoverage uses schema.org's spec'd shapes (issue #222):
        # Polygon → GeoShape with `box` ("south west north east").
        self.assertEqual(article["spatialCoverage"]["@type"], "Place")
        self.assertEqual(article["spatialCoverage"]["geo"]["@type"], "GeoShape")
        # Sulawesi extent: south=-5.7, west=119.0, north=1.7, east=125.0.
        self.assertEqual(article["spatialCoverage"]["geo"]["box"], "-5.7 119.0 1.7 125.0")
        # temporalCoverage with open-start interval, matching ISO 8601 "../end".
        self.assertEqual(article["temporalCoverage"], "../2024-12-31")
        # publisher derived from work.source.
        self.assertEqual(article["publisher"]["@type"], "Organization")
        self.assertEqual(article["publisher"]["name"], "SEO Test Journal")

    def test_jsonld_author_orcid_in_same_as(self):
        """Authors with an ORCID get sameAs: https://orcid.org/... in the JSON-LD."""
        from works.models import Author, WorkAuthor

        alice = Author.objects.create(name="Mariana Sontag-Gonzalez", orcid="0000-0002-1825-0097")
        WorkAuthor.objects.create(work=self.work, author=alice, rank=0)
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        article = next(
            (b for b in _find_jsonld(soup) if b.get("@type") == "ScholarlyArticle"),
            None,
        )
        self.assertIsNotNone(article)
        authors = article["author"]
        alice_entry = next((a for a in authors if a["name"] == "Mariana Sontag-Gonzalez"), None)
        self.assertIsNotNone(alice_entry, "Alice not found in schema.org author list")
        self.assertEqual(alice_entry["sameAs"], "https://orcid.org/0000-0002-1825-0097")

    def test_jsonld_author_without_orcid_has_no_same_as(self):
        """Authors without an ORCID must not get a sameAs field."""
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        article = next(
            (b for b in _find_jsonld(soup) if b.get("@type") == "ScholarlyArticle"),
            None,
        )
        self.assertIsNotNone(article)
        for person in article["author"]:
            self.assertNotIn("sameAs", person)

    def test_citation_meta_tags(self):
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        # citation_title, citation_doi, citation_journal_title, citation_publication_date,
        # citation_abstract_html_url, citation_issn — all expected on a published work.
        for name in (
            "citation_title",
            "citation_doi",
            "citation_journal_title",
            "citation_publication_date",
            "citation_abstract_html_url",
            "citation_issn",
        ):
            self.assertIsNotNone(
                soup.find("meta", attrs={"name": name}),
                f"missing citation_* tag: {name}",
            )
        # One citation_author per author, in order.
        authors = [t["content"] for t in soup.find_all("meta", attrs={"name": "citation_author"})]
        self.assertEqual(len(authors), 2)
        self.assertEqual(authors[0], "Mariana Sontag-Gonzalez")

    def test_citation_meta_tags_for_zotero(self):
        """Issue #243: tags Zotero's Embedded Metadata translator reads."""
        # Populate journal-citation fields and re-fetch.
        self.work.volume = "10"
        self.work.issue = "2"
        self.work.first_page = "123"
        self.work.last_page = "145"
        self.work.url = "https://example.test/article/1.pdf"
        self.work.openalex_id = "https://openalex.org/W123456789"
        self.work.save()

        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")

        def _content(name: str) -> str | None:
            tag = soup.find("meta", attrs={"name": name})
            return tag["content"] if tag else None

        self.assertEqual(_content("citation_abstract"), self.work.abstract)
        self.assertEqual(_content("citation_publisher"), "SEO Test Journal")
        self.assertEqual(_content("citation_language"), "en")
        self.assertEqual(_content("citation_volume"), "10")
        self.assertEqual(_content("citation_issue"), "2")
        self.assertEqual(_content("citation_firstpage"), "123")
        self.assertEqual(_content("citation_lastpage"), "145")
        self.assertEqual(_content("citation_pdf_url"), self.work.url)

        # Repeated citation_keywords, one per keyword and topic.
        kw_tags = [t["content"] for t in soup.find_all("meta", attrs={"name": "citation_keywords"})]
        self.assertEqual(kw_tags, ["Earth Sciences", "Geomagnetism and Paleomagnetism Studies"])

    def test_citation_pdf_url_omitted_when_url_is_not_pdf(self):
        # work.url ends in .../article/1 — no .pdf, so no citation_pdf_url.
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        self.assertIsNone(soup.find("meta", attrs={"name": "citation_pdf_url"}))

    def test_jsonld_nested_publication_volume_and_issue(self):
        self.work.volume = "10"
        self.work.issue = "2"
        self.work.save()
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        article = next(
            (b for b in _find_jsonld(soup) if b.get("@type") == "ScholarlyArticle"),
            None,
        )
        self.assertIsNotNone(article)
        is_part_of = article["isPartOf"]
        self.assertEqual(is_part_of["@type"], "PublicationIssue")
        self.assertEqual(is_part_of["issueNumber"], "2")
        self.assertEqual(is_part_of["isPartOf"]["@type"], "PublicationVolume")
        self.assertEqual(is_part_of["isPartOf"]["volumeNumber"], "10")
        self.assertEqual(is_part_of["isPartOf"]["isPartOf"]["@type"], "Periodical")
        self.assertEqual(is_part_of["isPartOf"]["isPartOf"]["name"], "SEO Test Journal")

    def test_jsonld_sameas_includes_openalex_when_set(self):
        self.work.openalex_id = "https://openalex.org/W123456789"
        self.work.save()
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        article = next(
            (b for b in _find_jsonld(soup) if b.get("@type") == "ScholarlyArticle"),
            None,
        )
        same_as = article["sameAs"]
        # When multiple, sameAs is a list; when only DOI, it's a string.
        self.assertIsInstance(same_as, list)
        self.assertIn(f"https://doi.org/{self.work.doi}", same_as)
        self.assertIn("https://openalex.org/W123456789", same_as)

    def test_jsonld_sameas_includes_openaire_when_matched(self):
        self.work.openalex_id = "https://openalex.org/W123456789"
        self.work.provenance = {
            "openaire_match": {
                "status": "matched",
                "openaire_id": "doi_dedup___::abc",
                "url": "https://explore.openaire.eu/search/result?id=doi_dedup___::abc",
            }
        }
        self.work.save()
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        article = next(
            (b for b in _find_jsonld(soup) if b.get("@type") == "ScholarlyArticle"),
            None,
        )
        same_as = article["sameAs"]
        self.assertIn("https://openalex.org/W123456789", same_as)
        self.assertIn("https://explore.openaire.eu/search/result?id=doi_dedup___::abc", same_as)

    def test_alternate_links_for_external_identifiers(self):
        self.work.openalex_id = "https://openalex.org/W123456789"
        self.work.provenance = {
            "openaire_match": {
                "status": "matched",
                "openaire_id": "doi_dedup___::abc",
                "url": "https://explore.openaire.eu/search/result?id=doi_dedup___::abc",
            }
        }
        self.work.save()
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        hrefs = {link.get("href") for link in soup.find_all("link", rel="alternate")}
        self.assertIn(f"https://doi.org/{self.work.doi}", hrefs)
        self.assertIn("https://openalex.org/W123456789", hrefs)
        self.assertIn("https://explore.openaire.eu/search/result?id=doi_dedup___::abc", hrefs)

    def test_coins_span_present(self):
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        spans = soup.find_all("span", class_="Z3988")
        self.assertEqual(len(spans), 1)
        title = spans[0].get("title", "")
        # COinS context object: keys unencoded, values percent-encoded.
        self.assertTrue(title.startswith("ctx_ver=Z39.88-2004"))
        self.assertIn("rft_val_fmt=info%3Aofi%2Ffmt%3Akev%3Amtx%3Ajournal", title)
        self.assertIn("rft.atitle=", title)
        self.assertIn("rft.au=", title)
        # DOI encoded as info:doi/<doi> — slashes and colons percent-encoded.
        self.assertIn(f"rft_id=info%3Adoi%2F{self.work.doi.replace('/', '%2F')}", title)

    def test_geo_meta_tags_present_for_geometry(self):
        # Issue #222: geo.position + ICBM emitted from the centroid, with the
        # two conventional separators (semicolon for geo.position, comma+space
        # for ICBM).
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        geo_pos = soup.find("meta", attrs={"name": "geo.position"})
        icbm = soup.find("meta", attrs={"name": "ICBM"})
        self.assertIsNotNone(geo_pos, "geo.position tag missing")
        self.assertIsNotNone(icbm, "ICBM tag missing")
        self.assertIn(";", geo_pos["content"])
        self.assertNotIn(";", icbm["content"])
        self.assertIn(", ", icbm["content"])
        # Sulawesi centroid roughly at lat=-2, lon=122.
        lat_str, lon_str = geo_pos["content"].split(";")
        self.assertAlmostEqual(float(lat_str), -2.0, places=0)
        self.assertAlmostEqual(float(lon_str), 122.0, places=0)
        # And ICBM uses the same centroid in "lat, lon" form.
        icbm_lat, icbm_lon = (s.strip() for s in icbm["content"].split(","))
        self.assertAlmostEqual(float(icbm_lat), float(lat_str))
        self.assertAlmostEqual(float(icbm_lon), float(lon_str))

    def test_geo_meta_tags_absent_without_geometry(self):
        no_geom = Work.objects.create(
            title="No-extent work",
            url="https://example.test/article/3",
            doi="10.1234/test.geo.none",
            source=self.work.source,
            status="p",
            geometry=GeometryCollection(),
        )
        resp = self.client.get(reverse("optimap:work-landing", args=[no_geom.get_identifier()]))
        soup = BeautifulSoup(resp.content, "html.parser")
        self.assertIsNone(soup.find("meta", attrs={"name": "geo.position"}))
        self.assertIsNone(soup.find("meta", attrs={"name": "ICBM"}))

    def test_geo_placename_and_region_tags_when_fields_set(self):
        # When the denormalized fields are populated (by the pre_save signal
        # or the backfill command), the corresponding meta tags appear.
        self.work.placename = "Sulawesi, Indonesia"
        self.work.save()
        self.work.countries.add(_indonesia())
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        placename = soup.find("meta", attrs={"name": "geo.placename"})
        region = soup.find("meta", attrs={"name": "geo.region"})
        self.assertIsNotNone(placename)
        self.assertIsNotNone(region)
        self.assertEqual(placename["content"], "Sulawesi, Indonesia")
        self.assertEqual(region["content"], "ID")

    def test_geo_placename_and_region_absent_when_fields_unset(self):
        # The two coordinate-based tags are still there, but the placename /
        # region tags are omitted when their backing fields are empty.
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        self.assertIsNone(soup.find("meta", attrs={"name": "geo.placename"}))
        self.assertIsNone(soup.find("meta", attrs={"name": "geo.region"}))

    def test_jsonld_geo_is_geocoordinates_for_single_point(self):
        # Single-point work → schema.org GeoCoordinates (latitude/longitude).
        from django.contrib.gis.geos import Point

        point_work = Work.objects.create(
            title="Single-point work",
            url="https://example.test/article/4",
            doi="10.1234/test.geo.point",
            source=self.work.source,
            status="p",
            geometry=GeometryCollection(Point(10.0, 50.0, srid=4326), srid=4326),
            publicationDate="2025-01-14",
        )
        resp = self.client.get(reverse("optimap:work-landing", args=[point_work.get_identifier()]))
        soup = BeautifulSoup(resp.content, "html.parser")
        article = next(
            (b for b in _find_jsonld(soup) if b.get("@type") == "ScholarlyArticle"),
            None,
        )
        self.assertIsNotNone(article)
        geo = article["spatialCoverage"]["geo"]
        self.assertEqual(geo["@type"], "GeoCoordinates")
        self.assertAlmostEqual(geo["latitude"], 50.0)
        self.assertAlmostEqual(geo["longitude"], 10.0)

    def test_jsonld_place_includes_placename_and_country_when_set(self):
        # When the new denormalized fields are populated, the JSON-LD Place
        # carries the placename + ISO country code as well.
        self.work.placename = "Sulawesi, Indonesia"
        self.work.save()
        self.work.countries.add(_indonesia())
        resp = self.client.get(self.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        article = next(
            (b for b in _find_jsonld(soup) if b.get("@type") == "ScholarlyArticle"),
            None,
        )
        self.assertEqual(article["spatialCoverage"]["name"], "Sulawesi, Indonesia")
        self.assertEqual(article["spatialCoverage"]["addressCountry"], "ID")

    def test_unpublished_work_returns_404(self):
        # Sanity: SEO context must not leak unpublished works to anonymous users.
        unpublished = Work.objects.create(
            title="Draft",
            source=self.work.source,
            status="d",
            geometry=GeometryCollection(),
        )
        resp = self.client.get(reverse("optimap:work-landing", args=[unpublished.get_identifier()]))
        self.assertEqual(resp.status_code, 404)


@override_settings(GEOCODE_WORKS_ON_SAVE=False)
class TemporalCoverageMetadataTests(TestCase):
    """Verify that single and multiple time periods are emitted correctly
    in every HTML-header metadata channel: schema.org JSON-LD
    (temporalCoverage), and DC.temporal meta tags (issue #26)."""

    def setUp(self):
        self.client = Client()

    def _soup(self, work):
        url = reverse("optimap:work-landing", args=[work.get_identifier()])
        return BeautifulSoup(self.client.get(url).content, "html.parser")

    def _article_jsonld(self, soup):
        blobs = _find_jsonld(soup)
        return next((b for b in blobs if b.get("@type") == "ScholarlyArticle"), None)

    def _dc_temporal(self, soup):
        return [t["content"] for t in soup.find_all("meta", attrs={"name": "DC.temporal"})]

    def test_single_period_temporalcoverage_is_string(self):
        """Single period → temporalCoverage string (not an array)."""
        work = _make_published_work(
            doi="10.1234/temporal.single",
            url="https://example.test/temporal-single",
            timeperiod_startdate=["2010"],
            timeperiod_enddate=["2020"],
        )
        article = self._article_jsonld(self._soup(work))
        self.assertIsNotNone(article)
        self.assertEqual(article["temporalCoverage"], "2010/2020")

    def test_multiple_periods_temporalcoverage_is_array(self):
        """Multiple periods → temporalCoverage JSON-LD array."""
        work = _make_published_work(
            doi="10.1234/temporal.multi",
            url="https://example.test/temporal-multi",
            timeperiod_startdate=["2010", "2018"],
            timeperiod_enddate=["2015", "2020"],
        )
        article = self._article_jsonld(self._soup(work))
        self.assertIsNotNone(article)
        tc = article["temporalCoverage"]
        self.assertIsInstance(tc, list, "multiple periods must produce a JSON-LD array")
        self.assertEqual(tc, ["2010/2015", "2018/2020"])

    def test_open_interval_period(self):
        """Open-start interval uses '..' per ISO 8601."""
        work = _make_published_work(
            doi="10.1234/temporal.open",
            url="https://example.test/temporal-open",
            timeperiod_startdate=[None],
            timeperiod_enddate=["2024-12-31"],
        )
        article = self._article_jsonld(self._soup(work))
        self.assertIsNotNone(article)
        self.assertEqual(article["temporalCoverage"], "../2024-12-31")

    def test_no_temporal_omits_temporalcoverage(self):
        """Works with no time period omit temporalCoverage entirely."""
        work = _make_published_work(
            doi="10.1234/temporal.none",
            url="https://example.test/temporal-none",
            timeperiod_startdate=None,
            timeperiod_enddate=None,
        )
        article = self._article_jsonld(self._soup(work))
        self.assertIsNotNone(article)
        self.assertNotIn("temporalCoverage", article)

    def test_single_period_dc_temporal_tag(self):
        """Single period → one DC.temporal meta tag."""
        work = _make_published_work(
            doi="10.1234/temporal.dc.single",
            url="https://example.test/temporal-dc-single",
            timeperiod_startdate=["2010"],
            timeperiod_enddate=["2020"],
        )
        tags = self._dc_temporal(self._soup(work))
        self.assertEqual(tags, ["2010/2020"])

    def test_multiple_periods_dc_temporal_tags(self):
        """Multiple periods → one DC.temporal meta tag per period."""
        work = _make_published_work(
            doi="10.1234/temporal.dc.multi",
            url="https://example.test/temporal-dc-multi",
            timeperiod_startdate=["2010", "2018"],
            timeperiod_enddate=["2015", "2020"],
        )
        tags = self._dc_temporal(self._soup(work))
        self.assertEqual(tags, ["2010/2015", "2018/2020"])

    def test_no_temporal_omits_dc_temporal_tag(self):
        """Works with no time period emit no DC.temporal tags."""
        work = _make_published_work(
            doi="10.1234/temporal.dc.none",
            url="https://example.test/temporal-dc-none",
            timeperiod_startdate=None,
            timeperiod_enddate=None,
        )
        tags = self._dc_temporal(self._soup(work))
        self.assertEqual(tags, [])

    def test_html_body_shows_all_periods(self):
        """All periods appear in the #work-timeperiods element in the page body."""
        work = _make_published_work(
            doi="10.1234/temporal.body",
            url="https://example.test/temporal-body",
            timeperiod_startdate=["2010", "2018"],
            timeperiod_enddate=["2015", "2020"],
        )
        soup = self._soup(work)
        container = soup.find(id="work-timeperiods")
        self.assertIsNotNone(container, "#work-timeperiods element missing")
        text = container.get_text(" ", strip=True)
        self.assertIn("2010", text)
        self.assertIn("2015", text)
        self.assertIn("2018", text)
        self.assertIn("2020", text)


class HomepageSEOTests(TestCase):
    def test_homepage_jsonld_website_with_searchaction(self):
        resp = self.client.get(reverse("optimap:main"))
        self.assertEqual(resp.status_code, 200)
        soup = BeautifulSoup(resp.content, "html.parser")
        blobs = _find_jsonld(soup)
        site = next((b for b in blobs if b.get("@type") == "WebSite"), None)
        self.assertIsNotNone(site, "WebSite JSON-LD missing on homepage")
        self.assertIn("potentialAction", site)
        self.assertEqual(site["potentialAction"]["@type"], "SearchAction")

    def test_homepage_og_image_uses_fallback(self):
        resp = self.client.get(reverse("optimap:main"))
        soup = BeautifulSoup(resp.content, "html.parser")
        og_image = soup.find("meta", attrs={"property": "og:image"})
        self.assertIsNotNone(og_image, "homepage must have og:image for social previews")
        self.assertIn("og-fallback.png", og_image["content"])


class WorkPreviewImageTests(TestCase):
    """The renderer fetches OSM tiles, so we patch ``StaticMap.render`` and
    just exercise our own composition / branding / cache-invalidation logic."""

    def setUp(self):
        self.client = Client()
        self.work = _make_published_work(doi="10.1234/test.seo.preview")
        self.preview_url = reverse("optimap:work-preview", args=[self.work.get_identifier()])

    @staticmethod
    def _fake_tile_image(*args, **kwargs):
        from PIL import Image

        return Image.new("RGB", (1200, 630), color=(200, 220, 240))

    def test_preview_endpoint_serves_png(self):
        # Patch staticmap so no network call is made.
        from PIL import Image as _Image

        with mock.patch("works.services.preview_image.StaticMap") as SM:
            instance = SM.return_value
            instance.render.return_value = _Image.new("RGB", (1200, 630), color=(200, 220, 240))
            resp = self.client.get(self.preview_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertIn("max-age=", resp.get("Cache-Control", ""))
        # Verify the body is actually a PNG with the expected dimensions.
        from io import BytesIO

        from PIL import Image

        buf = BytesIO(b"".join(resp.streaming_content))
        img = Image.open(buf)
        self.assertEqual(img.size, (1200, 630))

    def test_preview_404_for_work_without_geometry(self):
        no_geom = Work.objects.create(
            title="No-extent",
            doi="10.1234/test.seo.preview-noextent",
            source=self.work.source,
            status="p",
            geometry=GeometryCollection(),
        )
        url = reverse("optimap:work-preview", args=[no_geom.get_identifier()])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_post_save_invalidates_cached_preview(self):
        from works.services.preview_image import cache_path_for

        path = cache_path_for(self.work)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"old preview bytes")
        self.assertTrue(path.exists())
        # Saving the work should fire the post_save signal and unlink the cache.
        self.work.title = self.work.title + " (edited)"
        self.work.save()
        self.assertFalse(path.exists(), "post_save signal must invalidate the cached preview")


class FeedPageSEOTests(TestCase):
    """``CollectionPage`` JSON-LD on the regional feed pages."""

    @classmethod
    def setUpTestData(cls):
        # Seed a minimal Africa GlobalRegion so we don't depend on the
        # network-fetching ``load_global_regions`` command in the test DB.
        # The geometry is just a coarse bbox over Africa — good enough for
        # the route to resolve and the SEO context to assemble.
        from works.models import GlobalRegion

        africa_geojson = {
            "type": "MultiPolygon",
            "coordinates": [
                [
                    [
                        [-20.0, -36.0],
                        [55.0, -36.0],
                        [55.0, 38.0],
                        [-20.0, 38.0],
                        [-20.0, -36.0],
                    ]
                ]
            ],
        }
        cls.africa = GlobalRegion.objects.create(
            name="Africa",
            region_type=GlobalRegion.CONTINENT,
            geom=GEOSGeometry(json.dumps(africa_geojson), srid=4326),
        )

    def test_continent_feed_page_emits_collectionpage(self):
        from django.core.cache import cache

        cache.clear()  # the feed page caches by slug; flush so we don't read stale
        resp = self.client.get("/regions/continent/africa/")
        self.assertEqual(resp.status_code, 200)
        soup = BeautifulSoup(resp.content, "html.parser")
        blobs = _find_jsonld(soup)
        coll = next((b for b in blobs if b.get("@type") == "CollectionPage"), None)
        self.assertIsNotNone(coll, "CollectionPage JSON-LD missing on feed page")
        self.assertEqual(coll["about"]["@type"], "Place")
        self.assertEqual(coll["about"]["name"], "Africa")
        # Bounding box should round-trip from the GlobalRegion geometry.
        self.assertIn("box", coll["about"]["geo"])

    def test_feed_page_og_image_uses_fallback(self):
        from django.core.cache import cache

        cache.clear()
        resp = self.client.get("/regions/continent/africa/")
        soup = BeautifulSoup(resp.content, "html.parser")
        og_image = soup.find("meta", attrs={"property": "og:image"})
        self.assertIsNotNone(og_image, "feed page must have og:image for social previews")
        self.assertIn("og-fallback.png", og_image["content"])
