# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for works/harvesting/openalex_discovery.py — the "Suggest from
OpenAlex" lookup used by /manage/sources/new/."""

import responses
from django.test import TestCase

from works.harvesting.openalex_discovery import (
    OPENALEX_SOURCES_URL,
    OPENALEX_WORKS_URL,
    guess_doi_prefix,
    search_openalex_sources,
)


class SearchOpenalexSourcesTest(TestCase):
    def test_empty_query_returns_empty_list_without_a_request(self):
        self.assertEqual(search_openalex_sources(""), [])
        self.assertEqual(search_openalex_sources(None), [])

    @responses.activate
    def test_returns_candidates_from_results(self):
        responses.add(
            responses.GET,
            OPENALEX_SOURCES_URL,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/S123",
                        "display_name": "Example Journal",
                        "issn_l": "1234-5678",
                        "homepage_url": "https://example.org",
                        "works_count": 42,
                    },
                    {
                        "id": "https://openalex.org/S456",
                        "display_name": "Example Journal (alt)",
                        "issn_l": None,
                        "homepage_url": None,
                        "works_count": 3,
                    },
                ]
            },
        )
        candidates = search_openalex_sources("Example Journal")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["openalex_id"], "S123")
        self.assertEqual(candidates[0]["issn_l"], "1234-5678")
        self.assertEqual(candidates[0]["works_count"], 42)
        self.assertEqual(candidates[1]["openalex_id"], "S456")
        self.assertEqual(candidates[1]["issn_l"], "")

    @responses.activate
    def test_respects_limit(self):
        responses.add(
            responses.GET,
            OPENALEX_SOURCES_URL,
            json={
                "results": [{"id": f"https://openalex.org/S{i}", "display_name": f"Journal {i}"} for i in range(10)]
            },
        )
        candidates = search_openalex_sources("Journal", limit=3)
        self.assertEqual(len(candidates), 3)

    @responses.activate
    def test_network_error_returns_empty_list(self):
        responses.add(responses.GET, OPENALEX_SOURCES_URL, status=503)
        self.assertEqual(search_openalex_sources("Example Journal"), [])

    @responses.activate
    def test_item_without_id_is_skipped(self):
        responses.add(
            responses.GET,
            OPENALEX_SOURCES_URL,
            json={"results": [{"display_name": "No id here"}]},
        )
        self.assertEqual(search_openalex_sources("x"), [])


class GuessDoiPrefixTest(TestCase):
    def test_no_openalex_id_returns_none(self):
        self.assertIsNone(guess_doi_prefix(""))
        self.assertIsNone(guess_doi_prefix(None))

    @responses.activate
    def test_most_common_prefix_among_sampled_works(self):
        responses.add(
            responses.GET,
            OPENALEX_WORKS_URL,
            json={
                "results": [
                    {"doi": "https://doi.org/10.1190/abc123"},
                    {"doi": "https://doi.org/10.1190/def456"},
                    {"doi": "https://doi.org/10.9999/other"},
                ]
            },
        )
        self.assertEqual(guess_doi_prefix("S123"), "10.1190")

    @responses.activate
    def test_no_dois_returns_none(self):
        responses.add(responses.GET, OPENALEX_WORKS_URL, json={"results": [{"doi": None}, {}]})
        self.assertIsNone(guess_doi_prefix("S123"))

    @responses.activate
    def test_network_error_returns_none(self):
        responses.add(responses.GET, OPENALEX_WORKS_URL, status=503)
        self.assertIsNone(guess_doi_prefix("S123"))
