# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Issue #192 wrap-up: the generic OAI-PMH harvester auto-creates a
``Collection`` for each endpoint on first run if the admin hasn't pre-assigned
one. Existing Source rows without a Collection are backfilled by migration
``0007_backfill_collections_for_oai_sources``.

The auto-create logic lives in
``works.harvesting.common.ensure_collection_for_source`` and is wired into
``harvest_oai_endpoint`` (also used for ``ojs`` and ``janeway`` source types).
"""

from unittest.mock import Mock, patch

from django.test import TestCase

from works.harvesting.common import ensure_collection_for_source
from works.models import Collection, HarvestingEvent, Source


class EnsureCollectionForSourceTests(TestCase):
    """Direct unit tests on the helper."""

    def test_creates_collection_with_slug_derived_from_name(self):
        source = Source.objects.create(
            name="Earth System Science Data",
            url_field="https://essd.copernicus.org/oai/",
            source_type="oai-pmh",
        )
        collection = ensure_collection_for_source(source)
        self.assertIsNotNone(collection)
        self.assertEqual(collection.identifier, "earth-system-science-data")
        self.assertEqual(collection.name, "Earth System Science Data")
        # New collections start unpublished — admin reviews before exposing.
        self.assertFalse(collection.is_published)
        # Source FK is set and persisted.
        source.refresh_from_db()
        self.assertEqual(source.collection_id, collection.id)

    def test_returns_existing_collection_when_already_assigned(self):
        existing = Collection.objects.create(identifier="preassigned", name="Preassigned")
        source = Source.objects.create(
            name="Some Source",
            url_field="https://x.example/oai",
            source_type="oai-pmh",
            collection=existing,
        )
        result = ensure_collection_for_source(source)
        self.assertEqual(result, existing)
        # No new Collection was created.
        self.assertEqual(Collection.objects.count(), 1)

    def test_handles_slug_collision_with_numeric_suffix(self):
        Collection.objects.create(identifier="journal-x", name="Journal X (existing)")
        source = Source.objects.create(
            name="Journal X",
            url_field="https://jx.example/oai",
            source_type="oai-pmh",
        )
        collection = ensure_collection_for_source(source)
        self.assertEqual(collection.identifier, "journal-x-2")

    def test_skips_when_source_has_no_name(self):
        source = Source.objects.create(
            name="",
            url_field="https://noname.example/oai",
            source_type="oai-pmh",
        )
        result = ensure_collection_for_source(source)
        self.assertIsNone(result)
        self.assertEqual(Collection.objects.count(), 0)


class HarvestOaiEndpointAutoCreatesCollectionTests(TestCase):
    """Driving harvest_oai_endpoint without a pre-assigned collection should
    leave the source linked to a freshly-created one. Mocks the HTTP fetch
    and the parser — we're only testing the wiring."""

    def setUp(self):
        self.source = Source.objects.create(
            name="Demo OAI Journal",
            url_field="https://demo.example/oai",
            source_type="oai-pmh",
        )
        self.assertIsNone(self.source.collection)

    @patch("works.harvesting.sessions._oai_session")
    @patch("works.harvesting.oai._oai_session")
    @patch("works.harvesting.oai.parse_oai_xml_and_save_works")
    def test_first_harvest_creates_collection_and_links_source(
        self, mock_parser, mock_session_factory, mock_stats_session_factory
    ):
        from works.tasks import harvest_oai_endpoint

        # Bare-minimum OAI-PMH XML so the upstream sniff passes.
        fake_response = Mock()
        fake_response.ok = True
        fake_response.status_code = 200
        fake_response.headers = {"Content-Type": "application/xml"}
        fake_response.content = b"<OAI-PMH><ListRecords></ListRecords></OAI-PMH>"
        mock_session = Mock()
        mock_session.get.return_value = fake_response
        mock_session_factory.return_value = mock_session
        # complete_harvest()'s fetch_and_store_oai_works_count() imports
        # _oai_session from works.harvesting.sessions directly (not the
        # works.harvesting.oai re-export the harvester itself uses), so without
        # this second patch it makes a real HTTP request to the fake
        # `demo.example` host and burns ~10s per test on DNS-failure retries.
        mock_stats_session_factory.return_value = mock_session

        harvest_oai_endpoint(self.source.id)

        self.source.refresh_from_db()
        self.assertIsNotNone(self.source.collection)
        self.assertEqual(self.source.collection.identifier, "demo-oai-journal")
        # The new Collection is created unpublished — admin reviews first.
        self.assertFalse(self.source.collection.is_published)
        # And only one Collection exists overall.
        self.assertEqual(Collection.objects.count(), 1)
        # An event was still recorded (auto-create happens before event creation,
        # so the event has the new source.collection_id at creation time).
        self.assertTrue(HarvestingEvent.objects.filter(source=self.source).exists())

    @patch("works.harvesting.sessions._oai_session")
    @patch("works.harvesting.oai._oai_session")
    @patch("works.harvesting.oai.parse_oai_xml_and_save_works")
    def test_second_harvest_reuses_collection(self, mock_parser, mock_session_factory, mock_stats_session_factory):
        from works.tasks import harvest_oai_endpoint

        fake_response = Mock()
        fake_response.ok = True
        fake_response.status_code = 200
        fake_response.headers = {"Content-Type": "application/xml"}
        fake_response.content = b"<OAI-PMH><ListRecords></ListRecords></OAI-PMH>"
        mock_session = Mock()
        mock_session.get.return_value = fake_response
        mock_session_factory.return_value = mock_session
        # See test_first_harvest_creates_collection_and_links_source for why
        # this second patch is needed (fetch_and_store_oai_works_count imports
        # _oai_session from works.harvesting.sessions directly).
        mock_stats_session_factory.return_value = mock_session

        harvest_oai_endpoint(self.source.id)
        self.source.refresh_from_db()
        first_collection_id = self.source.collection_id

        harvest_oai_endpoint(self.source.id)
        self.source.refresh_from_db()
        self.assertEqual(
            self.source.collection_id, first_collection_id, "re-harvest must not create a second collection"
        )
        self.assertEqual(Collection.objects.count(), 1)

    @patch("works.harvesting.sessions._oai_session")
    @patch("works.harvesting.oai._oai_session")
    @patch("works.harvesting.oai.parse_oai_xml_and_save_works")
    def test_preassigned_collection_is_left_alone(self, mock_parser, mock_session_factory, mock_stats_session_factory):
        from works.tasks import harvest_oai_endpoint

        preassigned = Collection.objects.create(
            identifier="preassigned-feed",
            name="Pre-assigned feed",
            is_published=True,
        )
        self.source.collection = preassigned
        self.source.save(update_fields=["collection"])

        fake_response = Mock()
        fake_response.ok = True
        fake_response.status_code = 200
        fake_response.headers = {"Content-Type": "application/xml"}
        fake_response.content = b"<OAI-PMH><ListRecords></ListRecords></OAI-PMH>"
        mock_session = Mock()
        mock_session.get.return_value = fake_response
        mock_session_factory.return_value = mock_session
        # See test_first_harvest_creates_collection_and_links_source for why
        # this second patch is needed (fetch_and_store_oai_works_count imports
        # _oai_session from works.harvesting.sessions directly).
        mock_stats_session_factory.return_value = mock_session

        harvest_oai_endpoint(self.source.id)

        self.source.refresh_from_db()
        self.assertEqual(self.source.collection_id, preassigned.id, "pre-assigned collection must not be overwritten")
        # And it stays published — the admin's curation choice is respected.
        self.source.collection.refresh_from_db()
        self.assertTrue(self.source.collection.is_published)
