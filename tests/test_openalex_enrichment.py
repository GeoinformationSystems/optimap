# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for ``works.harvesting.openalex.build_openalex_fields``.

Regression coverage for narrowing its ``try/except`` to just the matcher call
(issue: a bare `except Exception` used to also swallow bugs in the field-
assembly logic that runs *after* a successful match, indistinguishable in the
logs from a genuine "no match"/"matcher unreachable" case).
"""

from unittest.mock import patch

from django.test import TestCase

from works.harvesting.openalex import build_openalex_fields


class BuildOpenalexFieldsMatcherFailureTests(TestCase):
    """A failure in the matcher call itself (network, matcher-internal bug)
    must still degrade gracefully — this is the genuinely "expected to
    sometimes fail" case.
    """

    @patch("works.harvesting.openalex.get_openalex_matcher")
    def test_matcher_call_raising_returns_no_match_fields(self, mock_get_matcher):
        mock_get_matcher.return_value.match_publication.side_effect = RuntimeError("matcher exploded")

        fields, provenance = build_openalex_fields(title="Some Title", doi="10.1/2")

        self.assertIsNone(fields["openalex_id"])
        self.assertEqual(provenance, {})

    @patch("works.harvesting.openalex.get_openalex_matcher")
    def test_matcher_call_raising_preserves_existing_metadata(self, mock_get_matcher):
        mock_get_matcher.return_value.match_publication.side_effect = RuntimeError("matcher exploded")

        fields, provenance = build_openalex_fields(
            title="Some Title",
            existing_metadata={"authors": ["A. Author"], "keywords": ["kw"]},
        )

        self.assertEqual(fields["authors"], ["A. Author"])
        self.assertEqual(fields["keywords"], ["kw"])
        self.assertEqual(provenance["authors"], "original_source")


class BuildOpenalexFieldsPostMatchBugTests(TestCase):
    """A bug in the field-assembly logic *after* a successful matcher call
    must now propagate rather than being silently logged as "matching
    failed" — this is the "unexpected, should raise" case the narrower
    try/except is meant to surface.
    """

    @patch("works.harvesting.openalex.get_openalex_matcher")
    def test_bug_in_openalex_data_shape_propagates(self, mock_get_matcher):
        # `.get(...)` on a list (instead of the expected dict) raises
        # AttributeError as soon as the field-assembly code touches it.
        mock_get_matcher.return_value.match_publication.return_value = (["not", "a", "dict"], None)

        with self.assertRaises(AttributeError):
            build_openalex_fields(title="Some Title", doi="10.1/2")

    @patch("works.harvesting.openalex.get_openalex_matcher")
    def test_bug_in_partial_matches_shape_propagates(self, mock_get_matcher):
        # With a real match plus a malformed partial_matches entry, the DOI
        # version-fallback code does partial_matches[0].get(...), which
        # raises when that entry isn't a dict — this used to be swallowed by
        # the old blanket try/except.
        mock_get_matcher.return_value.match_publication.return_value = ({"openalex_id": "W1"}, ["not-a-dict"])

        with self.assertRaises(AttributeError):
            build_openalex_fields(title="Some Title", doi="10.1/2")


class BuildOpenalexFieldsAuthorRecordsTests(TestCase):
    """Regression test: build_openalex_fields() must propagate the matcher's
    ORCID-bearing ``author_records`` into its returned fields dict.

    Before this fix, ``author_records`` was computed by
    ``OpenAlexMatcher.extract_openalex_fields`` but silently dropped here, so
    ``link_openalex_authors()`` — called by every harvester right after
    saving a work — always received an empty list and never linked any
    ``Author``/``WorkAuthor`` rows during regular harvesting. Only the
    OpenAlex-as-source harvester (which builds author_records itself,
    bypassing this function) and the manual ``backfill_author_orcids``
    command (which calls the OpenAlex API directly) actually worked.
    """

    @patch("works.harvesting.openalex.get_openalex_matcher")
    def test_author_records_propagated_on_match(self, mock_get_matcher):
        records = [{"name": "A. Author", "orcid": "0000-0001-2345-6789", "openalex_id": "A1", "rank": 0}]
        mock_get_matcher.return_value.match_publication.return_value = (
            {"openalex_id": "W1", "author_records": records},
            None,
        )

        fields, _provenance = build_openalex_fields(title="Some Title", doi="10.1/2")

        self.assertEqual(fields["author_records"], records)

    @patch("works.harvesting.openalex.get_openalex_matcher")
    def test_author_records_propagated_even_when_original_source_authors_win(self, mock_get_matcher):
        # The plain `authors` display list can come from the original source,
        # but the ORCID-bearing author_records (a separate, relational
        # concern — see works/services/authors.py) must still come through.
        records = [{"name": "A. Author", "orcid": "0000-0001-2345-6789", "openalex_id": "A1", "rank": 0}]
        mock_get_matcher.return_value.match_publication.return_value = (
            {"openalex_id": "W1", "authors": ["OpenAlex Name"], "author_records": records},
            None,
        )

        fields, provenance = build_openalex_fields(
            title="Some Title", doi="10.1/2", existing_metadata={"authors": ["Original Source Name"]}
        )

        self.assertEqual(fields["authors"], ["Original Source Name"])
        self.assertEqual(provenance["authors"], "original_source")
        self.assertEqual(fields["author_records"], records)

    @patch("works.harvesting.openalex.get_openalex_matcher")
    def test_no_author_records_key_when_matcher_returns_none(self, mock_get_matcher):
        mock_get_matcher.return_value.match_publication.return_value = ({"openalex_id": "W1"}, None)

        fields, _provenance = build_openalex_fields(title="Some Title", doi="10.1/2")

        self.assertNotIn("author_records", fields)
