# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for works/harvesting/discovery.py — OAI-PMH endpoint discovery used
by /manage/sources/new/ (issues #133, #44)."""

import responses
from django.test import TestCase

from works.harvesting.discovery import discover_oai_endpoint

_IDENTIFY_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-01-01T00:00:00Z</responseDate>
  <request verb="Identify">https://example.org/oai</request>
  <Identify>
    <repositoryName>Example Journal Repository</repositoryName>
    <baseURL>https://example.org/oai</baseURL>
    <protocolVersion>2.0</protocolVersion>
    <adminEmail>admin@example.org</adminEmail>
    <earliestDatestamp>2001-01-01</earliestDatestamp>
    <deletedRecord>no</deletedRecord>
    <granularity>YYYY-MM-DD</granularity>
  </Identify>
</OAI-PMH>"""

_NOT_OAI_HTML = b"<html><body>404 not found</body></html>"


class DiscoverOaiEndpointTest(TestCase):
    @responses.activate
    def test_finds_valid_endpoint_via_platform_pattern(self):
        responses.add(responses.GET, "https://example.org/journal", body="<html><head></head></html>")
        responses.add(
            responses.GET,
            "https://example.org/journal/oai",
            body="not-xml",
            status=404,
        )
        responses.add(
            responses.GET,
            "https://example.org/oai?verb=Identify",
            body=_IDENTIFY_XML,
            content_type="text/xml",
        )
        responses.add(
            responses.GET,
            "https://example.org/oai/request?verb=Identify",
            body=_NOT_OAI_HTML,
            status=404,
        )
        responses.add(
            responses.GET,
            "https://example.org/journal?verb=Identify",
            body=_NOT_OAI_HTML,
            status=404,
        )

        candidates = discover_oai_endpoint("https://example.org/journal")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["repository_name"], "Example Journal Repository")
        self.assertEqual(candidates[0]["base_url"], "https://example.org/oai")
        self.assertEqual(candidates[0]["earliest_datestamp"], "2001-01-01")

    @responses.activate
    def test_no_candidates_when_nothing_validates(self):
        responses.add(responses.GET, "https://example.org/dead", body="<html></html>")
        for path in ("/dead/oai", "/oai", "/oai/request", "/dead"):
            responses.add(responses.GET, f"https://example.org{path}", body=_NOT_OAI_HTML, status=404)

        candidates = discover_oai_endpoint("https://example.org/dead")
        self.assertEqual(candidates, [])

    def test_empty_url_returns_no_candidates(self):
        self.assertEqual(discover_oai_endpoint(""), [])

    @responses.activate
    def test_head_pointer_is_tried_first(self):
        html_with_pointer = (
            b'<html><head><link rel="alternate" type="application/xml+oai" href="/custom/oai/endpoint"></head></html>'
        )
        responses.add(responses.GET, "https://example.org/page", body=html_with_pointer)
        responses.add(
            responses.GET,
            "https://example.org/custom/oai/endpoint?verb=Identify",
            body=_IDENTIFY_XML,
            content_type="text/xml",
        )
        # Platform-pattern / self fallbacks should not even need to be reached in this
        # case, but register them so the test fails loudly (not with a real request)
        # if the head-pointer candidate were skipped.
        responses.add(responses.GET, "https://example.org/page/oai?verb=Identify", body=_NOT_OAI_HTML, status=404)
        responses.add(responses.GET, "https://example.org/oai?verb=Identify", body=_NOT_OAI_HTML, status=404)
        responses.add(responses.GET, "https://example.org/oai/request?verb=Identify", body=_NOT_OAI_HTML, status=404)
        responses.add(responses.GET, "https://example.org/page?verb=Identify", body=_NOT_OAI_HTML, status=404)

        candidates = discover_oai_endpoint("https://example.org/page")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["base_url"], "https://example.org/oai")

    @responses.activate
    def test_follows_redirect_and_uses_resolved_url_for_platform_patterns(self):
        # A bare domain (e.g. "josis.org") 302s to the real OJS journal path;
        # the OJS /index.php/<journal>/oai pattern only matches against that
        # resolved path, not the original bare-domain URL.
        responses.add(
            responses.GET,
            "https://example.org/",
            status=302,
            headers={"Location": "https://example.org/index.php/journal"},
        )
        responses.add(
            responses.GET,
            "https://example.org/index.php/journal",
            body="<html><head></head></html>",
        )
        responses.add(
            responses.GET,
            "https://example.org/index.php/journal/oai?verb=Identify",
            body=_IDENTIFY_XML,
            content_type="text/xml",
        )
        for path in ("/oai", "/oai/request", "/index.php/journal"):
            responses.add(responses.GET, f"https://example.org{path}?verb=Identify", body=_NOT_OAI_HTML, status=404)

        candidates = discover_oai_endpoint("example.org")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["base_url"], "https://example.org/oai")
