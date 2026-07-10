# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.test.utils import override_settings

from works.models import HarvestingEvent, Source, Work
from works.tasks import harvest_oai_endpoint

User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class HarvestRegularMetadataTestCase(TestCase):
    def setUp(self):
        Work.objects.all().delete()
        HarvestingEvent.objects.all().delete()

        self.user = User.objects.create_user(username="testuser", email="testuser@example.com", password="password123")
        # No Collection is pre-assigned here on purpose: this test exercises
        # the unset-collection branch of harvest_oai_endpoint, which now
        # auto-creates one on first run via ensure_collection_for_source.
        # The completion email asserts below verify that the email's
        # "Collection used:" line falls back to the source name when the
        # auto-create has just produced a same-named Collection.
        self.source = Source.objects.create(
            name="Test Source",
            # Explicit date bounds bypass year-chunking; without them the
            # harvester would generate one chunk per year since 1970 and call
            # the mocked parser 57 times, creating duplicate DOIs.
            url_field="https://example.com/oai?verb=ListRecords&metadataPrefix=oai_dc&from=2024-01-01&until=2024-12-31",
            tags="test,harvest",
        )

    @patch("works.harvesting.oai._oai_session")
    @patch("works.harvesting.oai.parse_oai_xml_and_save_works")
    def test_harvest_regular_metadata_sends_email(self, mock_parser, mock_session_factory):
        fake_response = Mock()
        fake_response.ok = True
        fake_response.status_code = 200
        fake_response.headers = {"Content-Type": "application/xml"}
        fake_response.content = b"<OAI-PMH><ListRecords></ListRecords></OAI-PMH>"
        mock_session = Mock()
        mock_session.get.return_value = fake_response
        mock_session_factory.return_value = mock_session

        def fake_parser_func(
            content, event, max_records=None, warning_collector=None, update_existing=False, stats=None, **_kw
        ):
            Work.objects.create(
                title="Test Publication 1",
                doi="10.1000/1",
                job=event,
                timeperiod_startdate=[],
                timeperiod_enddate=[],
                geometry=None,
            )
            Work.objects.create(
                title="Test Publication 2",
                doi="10.1000/2",
                job=event,
                timeperiod_startdate=[],
                timeperiod_enddate=[],
                geometry=None,
            )
            if stats is not None:
                stats.created += 2
            return stats

        mock_parser.side_effect = fake_parser_func

        mail.outbox = []

        harvest_oai_endpoint(self.source.id, self.user)

        event = HarvestingEvent.objects.filter(source=self.source).latest("started_at")

        new_count = Work.objects.filter(job=event).count()
        self.assertEqual(new_count, 2, "Two publications should be created during harvest.")

        spatial_count = Work.objects.filter(job=event).exclude(geometry__isnull=True).count()
        self.assertEqual(spatial_count, 0, "No publication should have spatial metadata.")

        temporal_count = Work.objects.filter(job=event).exclude(timeperiod_startdate=[]).count()
        self.assertEqual(temporal_count, 0, "No publication has temporal metadata.")

        self.assertEqual(len(mail.outbox), 1, "One email should be sent to the user.")
        email = mail.outbox[0]
        self.assertIn("Harvesting Completed", email.subject)
        self.assertIn("Number of added articles: 2", email.body)
        self.assertIn("Number of articles with spatial metadata: 0", email.body)
        self.assertIn("Number of articles with temporal metadata: 0", email.body)
        # No collection set on this source — email falls back to the source name.
        self.assertIn(self.source.name, email.body)
        self.assertIn(self.source.url_field, email.body)
        self.assertIn(event.started_at.strftime("%Y-%m-%d"), email.body)
        self.assertIn(f"/manage/harvests/{event.id}/", email.body)
