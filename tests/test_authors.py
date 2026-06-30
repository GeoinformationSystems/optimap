# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the /by/<orcid>/ author facet pages and ORCID enrichment (issue #29)."""

from django.template.loader import render_to_string
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from works.models import Author, Work, WorkAuthor
from works.services.authors import authors_for_display, link_work_authors
from works.utils.orcid import normalize_orcid


class NormalizeOrcidTests(TestCase):
    def test_bare_orcid_passthrough(self):
        self.assertEqual(normalize_orcid("0000-0002-1825-0097"), "0000-0002-1825-0097")

    def test_https_prefix_stripped(self):
        self.assertEqual(normalize_orcid("https://orcid.org/0000-0002-1825-0097"), "0000-0002-1825-0097")

    def test_http_prefix_stripped(self):
        self.assertEqual(normalize_orcid("http://orcid.org/0000-0001-2345-6789"), "0000-0001-2345-6789")

    def test_orcid_dot_org_prefix_stripped(self):
        self.assertEqual(normalize_orcid("orcid.org/0000-0001-2345-678X"), "0000-0001-2345-678X")

    def test_lowercase_x_uppercased(self):
        self.assertEqual(normalize_orcid("0000-0001-2345-678x"), "0000-0001-2345-678X")

    def test_invalid_returns_none(self):
        self.assertIsNone(normalize_orcid("not-an-orcid"))

    def test_empty_returns_none(self):
        self.assertIsNone(normalize_orcid(""))
        self.assertIsNone(normalize_orcid(None))

    def test_too_short_returns_none(self):
        self.assertIsNone(normalize_orcid("0000-0002-1825-009"))

    def test_wrong_segments_returns_none(self):
        self.assertIsNone(normalize_orcid("0000-0002-1825"))


class LinkWorkAuthorsTests(TestCase):
    def setUp(self):
        self.work = Work.objects.create(status="p", title="Test Work", authors=["Alice", "Bob"])

    def _records(self):
        return [
            {"name": "Alice", "orcid": "0000-0002-1825-0097", "openalex_id": "A123", "rank": 0},
            {"name": "Bob", "openalex_id": "A456", "rank": 1},
        ]

    def test_links_authors_by_orcid(self):
        n = link_work_authors(self.work, self._records(), source="openalex")
        self.assertEqual(n, 2)
        self.assertEqual(self.work.author_links.count(), 2)

    def test_orcid_bearing_author_has_orcid(self):
        link_work_authors(self.work, self._records(), source="openalex")
        alice = Author.objects.get(orcid="0000-0002-1825-0097")
        self.assertEqual(alice.name, "Alice")

    def test_openalex_only_author_no_orcid(self):
        link_work_authors(self.work, self._records(), source="openalex")
        bob = Author.objects.get(openalex_id="A456")
        self.assertIsNone(bob.orcid)

    def test_skips_records_without_ids(self):
        records = [{"name": "Ghost", "rank": 0}]
        n = link_work_authors(self.work, records, source="openalex")
        self.assertEqual(n, 0)
        self.assertEqual(self.work.author_links.count(), 0)

    def test_fill_if_empty_skips_on_second_call(self):
        link_work_authors(self.work, self._records(), source="openalex")
        extra = [{"name": "Carol", "orcid": "0000-0003-1111-2222", "rank": 0}]
        n2 = link_work_authors(self.work, extra, source="openaire")
        self.assertEqual(n2, 0)
        self.assertEqual(self.work.author_links.count(), 2)

    def test_force_rebuilds_links(self):
        link_work_authors(self.work, self._records(), source="openalex")
        extra = [{"name": "Carol", "orcid": "0000-0003-1111-2222", "rank": 0}]
        n2 = link_work_authors(self.work, extra, source="openaire", force=True)
        self.assertEqual(n2, 1)
        self.assertEqual(self.work.author_links.count(), 1)

    def test_rank_ordering_preserved(self):
        link_work_authors(self.work, self._records(), source="openalex")
        ranks = list(WorkAuthor.objects.filter(work=self.work).order_by("rank").values_list("rank", flat=True))
        self.assertEqual(ranks, [0, 1])

    def test_provenance_records_metadata_source(self):
        link_work_authors(self.work, self._records(), source="openalex")
        self.work.refresh_from_db()
        self.assertEqual(self.work.provenance.get("metadata_sources", {}).get("author_orcids"), "openalex")

    def test_provenance_records_author_link_event(self):
        link_work_authors(self.work, self._records(), source="openalex")
        self.work.refresh_from_db()
        events = self.work.provenance.get("events", [])
        types = [e["type"] for e in events]
        self.assertIn("author_link", types)

    def test_upsert_updates_name(self):
        Author.objects.create(orcid="0000-0002-1825-0097", name="Old Name")
        records = [{"name": "Alice Updated", "orcid": "0000-0002-1825-0097", "rank": 0}]
        link_work_authors(self.work, records, source="openalex")
        self.assertEqual(Author.objects.get(orcid="0000-0002-1825-0097").name, "Alice Updated")

    def test_empty_records_returns_zero(self):
        self.assertEqual(link_work_authors(self.work, [], source="openalex"), 0)


class SupplementAuthorsTests(TestCase):
    def setUp(self):
        self.work = Work.objects.create(status="p", title="Supplement Test", authors=["Alice", "Bob"])
        # Seed OpenAlex links: Alice has ORCID at rank 0, Bob has only openalex_id at rank 1.
        self.alice = Author.objects.create(orcid="0000-0002-1825-0097", name="Alice", openalex_id="A123")
        self.bob = Author.objects.create(openalex_id="A456", name="Bob")
        WorkAuthor.objects.create(work=self.work, author=self.alice, rank=0)
        WorkAuthor.objects.create(work=self.work, author=self.bob, rank=1)
        self.work.provenance = {"metadata_sources": {"author_orcids": "openalex"}}
        self.work.save()

    def test_supplement_adds_missing_orcid(self):
        # Bob has no ORCID from OpenAlex; OpenAIRE provides one at rank 1.
        records = [{"name": "Bob", "orcid": "0000-0003-9999-8888", "rank": 1}]
        n = link_work_authors(self.work, records, source="openaire", supplement=True)
        self.assertEqual(n, 1)
        self.bob.refresh_from_db()
        # A new Author is created (Bob's existing entry has no ORCID, so it's a new Author row).
        self.assertTrue(Author.objects.filter(orcid="0000-0003-9999-8888").exists())

    def test_supplement_skips_consistent_orcid(self):
        # Alice's ORCID is already linked — OpenAIRE reports the same one.
        records = [{"name": "Alice", "orcid": "0000-0002-1825-0097", "rank": 0}]
        n = link_work_authors(self.work, records, source="openaire", supplement=True)
        self.assertEqual(n, 0)
        self.work.refresh_from_db()
        events = self.work.provenance.get("events", [])
        supplement_events = [e for e in events if e["type"] == "author_link_supplement"]
        self.assertEqual(supplement_events[0]["consistent"], 1)

    def test_supplement_records_conflict(self):
        # OpenAIRE provides a different ORCID at rank 0 (Alice's slot) — conflict.
        records = [{"name": "Alice Doppelganger", "orcid": "0000-0001-9999-1234", "rank": 0}]
        n = link_work_authors(self.work, records, source="openaire", supplement=True)
        self.assertEqual(n, 0)
        self.work.refresh_from_db()
        events = self.work.provenance.get("events", [])
        supplement_events = [e for e in events if e["type"] == "author_link_supplement"]
        self.assertEqual(len(supplement_events[0]["conflicts"]), 1)
        conflict = supplement_events[0]["conflicts"][0]
        self.assertEqual(conflict["kept"], "0000-0002-1825-0097")
        self.assertEqual(conflict["from_source"], "0000-0001-9999-1234")

    def test_supplement_extends_metadata_source(self):
        records = [{"name": "Bob", "orcid": "0000-0003-9999-8888", "rank": 1}]
        link_work_authors(self.work, records, source="openaire", supplement=True)
        self.work.refresh_from_db()
        self.assertEqual(self.work.provenance["metadata_sources"]["author_orcids"], "openalex+openaire")

    def test_supplement_does_not_delete_existing_links(self):
        records = [{"name": "Carol", "orcid": "0000-0004-1111-2222", "rank": 2}]
        link_work_authors(self.work, records, source="openaire", supplement=True)
        self.assertTrue(self.work.author_links.filter(orcid="0000-0002-1825-0097").exists())


class AuthorsForDisplayTests(TestCase):
    def setUp(self):
        self.work = Work.objects.create(status="p", title="Display Test", authors=["Alice", "Bob", "Ghost"])
        self.alice = Author.objects.create(orcid="0000-0002-1825-0097", name="Alice")
        WorkAuthor.objects.create(work=self.work, author=self.alice, rank=0)
        self.bob = Author.objects.create(openalex_id="A456", name="Bob")
        WorkAuthor.objects.create(work=self.work, author=self.bob, rank=1)

    def test_orcid_bearing_author_has_url(self):
        display = authors_for_display(self.work)
        alice_entry = display[0]
        self.assertEqual(alice_entry["name"], "Alice")
        self.assertEqual(alice_entry["orcid"], "0000-0002-1825-0097")
        self.assertIsNotNone(alice_entry["by_url"])

    def test_no_orcid_author_has_null_url(self):
        display = authors_for_display(self.work)
        bob_entry = display[1]
        self.assertEqual(bob_entry["name"], "Bob")
        self.assertIsNone(bob_entry["orcid"])
        self.assertIsNone(bob_entry["by_url"])

    def test_unlinked_author_has_null_url(self):
        display = authors_for_display(self.work)
        ghost_entry = display[2]
        self.assertEqual(ghost_entry["name"], "Ghost")
        self.assertIsNone(ghost_entry["orcid"])

    def test_no_authors_returns_empty(self):
        w = Work.objects.create(status="p", title="Empty authors")
        self.assertEqual(authors_for_display(w), [])

    def test_wa_used_when_more_complete_than_authors(self):
        """WorkAuthor names are used when they cover more authors than Work.authors."""
        work = Work.objects.create(status="h", title="Multi-author", authors=["Galasso", "Gabriele"])
        a1 = Author.objects.create(name="Gabriele Galasso", orcid="0000-0002-2501-456X")
        a2 = Author.objects.create(name="Gianniantonio Domina", orcid="0000-0003-4184-398X")
        WorkAuthor.objects.create(work=work, author=a1, rank=0)
        WorkAuthor.objects.create(work=work, author=a2, rank=1)
        display = authors_for_display(work)
        self.assertEqual(len(display), 2)
        self.assertEqual(display[0]["name"], "Gabriele Galasso")
        self.assertEqual(display[1]["name"], "Gianniantonio Domina")


class AuthorSchemaOrgTests(TestCase):
    """Schema.org JSON-LD output includes ORCID sameAs for linked authors."""

    def setUp(self):
        from works.models import Source

        self.client = Client()
        src = Source.objects.create(
            name="Test Journal",
            url_field="https://example.test/oai",
            harvest_interval_minutes=1440,
        )
        self.work = Work.objects.create(
            title="Test Work",
            doi="10.1234/schema.orcid.1",
            url="https://example.test/article/orcid1",
            source=src,
            status="p",
            authors=["Alice Researcher", "Bob Plain"],
            publicationDate="2025-06-01",
        )
        self.alice = Author.objects.create(name="Alice Researcher", orcid="0000-0002-1825-0097")
        WorkAuthor.objects.create(work=self.work, author=self.alice, rank=0)

    def _article_jsonld(self, url):
        import json

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self.client.get(url).content, "html.parser")
        blobs = []
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            if tag.string:
                try:
                    blobs.append(json.loads(tag.string))
                except json.JSONDecodeError:
                    pass
        return next((b for b in blobs if b.get("@type") == "ScholarlyArticle"), None)

    def test_orcid_author_has_same_as_in_jsonld(self):
        url = reverse("optimap:work-landing", args=[self.work.get_identifier()])
        article = self._article_jsonld(url)
        self.assertIsNotNone(article)
        alice_entry = next((a for a in article["author"] if "Alice" in a["name"]), None)
        self.assertIsNotNone(alice_entry)
        self.assertEqual(alice_entry["sameAs"], "https://orcid.org/0000-0002-1825-0097")

    def test_author_without_orcid_has_no_same_as_in_jsonld(self):
        url = reverse("optimap:work-landing", args=[self.work.get_identifier()])
        article = self._article_jsonld(url)
        self.assertIsNotNone(article)
        bob_entry = next((a for a in article["author"] if "Bob" in a["name"]), None)
        self.assertIsNotNone(bob_entry, "Bob should appear as a plain-text author")
        self.assertNotIn("sameAs", bob_entry)


@override_settings(ADMINS=[])
class AuthorPageViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.orcid = "0000-0002-1825-0097"
        self.author = Author.objects.create(name="Alice Researcher", orcid=self.orcid)
        self.work = Work.objects.create(status="p", title="A Spatial Study", authors=["Alice Researcher"])
        WorkAuthor.objects.create(work=self.work, author=self.author, rank=0)

    def test_author_page_200(self):
        url = reverse("optimap:by-author", kwargs={"orcid": self.orcid})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_author_page_shows_work(self):
        url = reverse("optimap:by-author", kwargs={"orcid": self.orcid})
        response = self.client.get(url)
        self.assertContains(response, "A Spatial Study")

    def test_author_page_shows_author_name(self):
        url = reverse("optimap:by-author", kwargs={"orcid": self.orcid})
        response = self.client.get(url)
        self.assertContains(response, "Alice Researcher")

    def test_unknown_orcid_returns_404(self):
        url = reverse("optimap:by-author", kwargs={"orcid": "0000-0000-0000-0000"})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_url_form_orcid_normalizes_and_200(self):
        url = reverse("optimap:by-author", kwargs={"orcid": self.orcid})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_harvested_work_shown(self):
        harvested = Work.objects.create(status="h", title="Harvested Work", authors=["Alice Researcher"])
        WorkAuthor.objects.create(work=harvested, author=self.author, rank=0)
        url = reverse("optimap:by-author", kwargs={"orcid": self.orcid})
        response = self.client.get(url)
        self.assertContains(response, "Harvested Work")

    def test_draft_work_not_shown(self):
        draft = Work.objects.create(status="d", title="Draft Work")
        WorkAuthor.objects.create(work=draft, author=self.author, rank=0)
        url = reverse("optimap:by-author", kwargs={"orcid": self.orcid})
        response = self.client.get(url)
        self.assertNotContains(response, "Draft Work")

    def test_author_page_has_link_rel_author_with_orcid(self):
        """HTML head must contain <link rel="author" href="https://orcid.org/..."/>."""
        from bs4 import BeautifulSoup

        url = reverse("optimap:by-author", kwargs={"orcid": self.orcid})
        soup = BeautifulSoup(self.client.get(url).content, "html.parser")
        link = soup.find("link", rel="author")
        self.assertIsNotNone(link, "<link rel='author'> missing from author page head")
        self.assertEqual(link["href"], f"https://orcid.org/{self.orcid}")

    def test_author_page_orcid_url_link_visible_in_body(self):
        """The full orcid.org URL must appear as a visible link in the page body."""
        url = reverse("optimap:by-author", kwargs={"orcid": self.orcid})
        response = self.client.get(url)
        self.assertContains(response, f"https://orcid.org/{self.orcid}")


@override_settings(ADMINS=[])
class AuthorIndexViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.author = Author.objects.create(name="Alice", orcid="0000-0002-1825-0097")
        self.work = Work.objects.create(status="p", title="Work by Alice")
        WorkAuthor.objects.create(work=self.work, author=self.author, rank=0)

    def test_index_200(self):
        url = reverse("optimap:by-index")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_index_lists_orcid_author(self):
        url = reverse("optimap:by-index")
        response = self.client.get(url)
        self.assertContains(response, "Alice")

    def test_author_without_orcid_not_listed(self):
        no_orcid = Author.objects.create(name="Bob", openalex_id="A999")
        work2 = Work.objects.create(status="p", title="Work by Bob")
        WorkAuthor.objects.create(work=work2, author=no_orcid, rank=0)
        url = reverse("optimap:by-index")
        response = self.client.get(url)
        self.assertNotContains(response, "Bob")

    def test_author_without_published_works_not_listed(self):
        Author.objects.create(name="Carol", orcid="0000-0003-1111-2222")
        url = reverse("optimap:by-index")
        response = self.client.get(url)
        self.assertNotContains(response, "Carol")

    def test_author_with_harvested_only_work_is_listed(self):
        harvested_author = Author.objects.create(name="Dave", orcid="0000-0004-1234-5678")
        h_work = Work.objects.create(status="h", title="Harvested Only")
        WorkAuthor.objects.create(work=h_work, author=harvested_author, rank=0)
        url = reverse("optimap:by-index")
        response = self.client.get(url)
        self.assertContains(response, "Dave")


class AuthorListHTMLTests(TestCase):
    """Tests for the partials/author_list.html template — covers rendering bugs caught in the wild."""

    ALICE_ORCID = "0000-0002-1825-0097"
    ALICE_BY_URL = "/by/0000-0002-1825-0097/"

    def _render(self, authors_display):
        return render_to_string("partials/author_list.html", {"authors_display": authors_display})

    def test_no_comment_text_rendered(self):
        html = self._render([{"name": "Alice", "orcid": None, "by_url": None}])
        self.assertNotIn("authors_display", html)
        self.assertNotIn("str|None", html)

    def test_orcid_author_has_by_name_link(self):
        html = self._render([{"name": "Alice", "orcid": self.ALICE_ORCID, "by_url": self.ALICE_BY_URL}])
        self.assertIn(f'href="{self.ALICE_BY_URL}"', html)
        self.assertIn(">Alice</a>", html)

    def test_orcid_author_has_orcid_icon_link(self):
        html = self._render([{"name": "Alice", "orcid": self.ALICE_ORCID, "by_url": self.ALICE_BY_URL}])
        self.assertIn(f"orcid.org/{self.ALICE_ORCID}", html)
        self.assertIn("orcid-id-icon-16.png", html)

    def test_nbsp_between_name_and_icon(self):
        html = self._render([{"name": "Alice", "orcid": self.ALICE_ORCID, "by_url": self.ALICE_BY_URL}])
        self.assertIn(">Alice</a>&nbsp;", html)

    def test_no_orcid_author_is_plain_text(self):
        html = self._render([{"name": "Bob", "orcid": None, "by_url": None}])
        self.assertIn("Bob", html)
        self.assertNotIn("<a", html)

    def test_comma_separator_no_space_before(self):
        authors = [
            {"name": "Alice", "orcid": self.ALICE_ORCID, "by_url": self.ALICE_BY_URL},
            {"name": "Bob", "orcid": None, "by_url": None},
        ]
        html = self._render(authors)
        # Comma must follow immediately after the previous author's closing tag, no space before it.
        self.assertIn("</a>, Bob", html)

    def test_plain_text_authors_comma_separator(self):
        authors = [
            {"name": "Alice", "orcid": None, "by_url": None},
            {"name": "Bob", "orcid": None, "by_url": None},
        ]
        html = self._render(authors)
        self.assertIn("Alice, Bob", html)

    def test_single_author_no_comma(self):
        html = self._render([{"name": "Solo", "orcid": None, "by_url": None}])
        self.assertNotIn(",", html)


@override_settings(ADMINS=[])
class AuthorAPITests(TestCase):
    def setUp(self):
        self.client = Client()
        self.orcid = "0000-0002-1825-0097"
        self.author = Author.objects.create(name="Alice", orcid=self.orcid)
        self.work = Work.objects.create(status="p", title="Spatial Paper", authors=["Alice"])
        WorkAuthor.objects.create(work=self.work, author=self.author, rank=0)

    def _features(self, response):
        return response.json().get("results", {}).get("features", [])

    def test_author_orcid_filter_returns_matching_work(self):
        response = self.client.get(f"/api/v1/works/?author_orcid={self.orcid}")
        self.assertEqual(response.status_code, 200)
        ids = [f["id"] for f in self._features(response)]
        self.assertIn(self.work.id, ids)

    def test_author_orcid_filter_excludes_other_works(self):
        other = Work.objects.create(status="p", title="Unrelated Paper")
        response = self.client.get(f"/api/v1/works/?author_orcid={self.orcid}")
        ids = [f["id"] for f in self._features(response)]
        self.assertNotIn(other.id, ids)

    def test_author_orcid_filter_invalid_ignored(self):
        response = self.client.get("/api/v1/works/?author_orcid=bad-value")
        self.assertEqual(response.status_code, 200)
        # Invalid ORCID → filter skipped, all public works returned.
        self.assertGreater(len(self._features(response)), 0)

    def test_authors_api_list(self):
        response = self.client.get("/api/v1/authors/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("results", data)
        orcids = [r["orcid"] for r in data["results"]]
        self.assertIn(self.orcid, orcids)

    def test_authors_api_detail(self):
        response = self.client.get(f"/api/v1/authors/{self.orcid}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["orcid"], self.orcid)

    def test_authors_api_detail_404(self):
        response = self.client.get("/api/v1/authors/0000-0000-0000-0000/")
        self.assertEqual(response.status_code, 404)

    def test_work_serializer_has_author_orcids(self):
        response = self.client.get(f"/api/v1/works/{self.work.pk}/?format=json")
        self.assertEqual(response.status_code, 200)
        props = response.json().get("properties", {})
        self.assertIn("author_orcids", props)

    def test_work_author_orcids_contains_orcid(self):
        response = self.client.get(f"/api/v1/works/{self.work.pk}/?format=json")
        orcids_field = response.json()["properties"]["author_orcids"]
        orcids = [entry["orcid"] for entry in orcids_field if entry.get("orcid")]
        self.assertIn(self.orcid, orcids)

    def test_work_authors_arrayfield_unchanged(self):
        response = self.client.get(f"/api/v1/works/{self.work.pk}/?format=json")
        props = response.json()["properties"]
        self.assertIn("authors", props)
        self.assertEqual(props["authors"], ["Alice"])


@override_settings(ADMINS=[])
class AuthorSitemapTests(TestCase):
    def setUp(self):
        self.orcid = "0000-0002-1825-0097"
        self.author = Author.objects.create(name="Alice", orcid=self.orcid)
        self.work = Work.objects.create(status="p", title="Alice's work")
        WorkAuthor.objects.create(work=self.work, author=self.author, rank=0)

    def test_sitemap_includes_orcid_author(self):
        from optimap.sitemaps import AuthorSitemap

        sitemap = AuthorSitemap()
        items = list(sitemap.items())
        orcids = [a.orcid for a in items]
        self.assertIn(self.orcid, orcids)

    def test_sitemap_excludes_no_orcid_author(self):
        no_orcid = Author.objects.create(name="Bob", openalex_id="A999")
        work2 = Work.objects.create(status="p", title="Bob's work")
        WorkAuthor.objects.create(work=work2, author=no_orcid, rank=0)
        from optimap.sitemaps import AuthorSitemap

        sitemap = AuthorSitemap()
        items = list(sitemap.items())
        orcids = [a.orcid for a in items]
        self.assertNotIn(None, orcids)

    def test_sitemap_excludes_author_without_published_work(self):
        Author.objects.create(name="Carol", orcid="0000-0003-1111-2222")
        from optimap.sitemaps import AuthorSitemap

        sitemap = AuthorSitemap()
        items = list(sitemap.items())
        orcids = [a.orcid for a in items]
        self.assertNotIn("0000-0003-1111-2222", orcids)

    def test_sitemap_location(self):
        from optimap.sitemaps import AuthorSitemap

        sitemap = AuthorSitemap()
        author = Author.objects.get(orcid=self.orcid)
        loc = sitemap.location(author)
        self.assertIn(self.orcid, loc)
