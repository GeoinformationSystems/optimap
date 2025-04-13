import os
import django
import subprocess
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

from django.test import TransactionTestCase
from django.utils import timezone
from django.core import mail
from django.conf import settings
from django.contrib.auth import get_user_model
from unittest.mock import patch, Mock
from django.test.utils import override_settings
from publications.models import Source, Publication, HarvestingEvent
from publications.tasks import harvest_regular_metadata_from_source

User = get_user_model()

@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class HarvestRegularMetadataTestCase(TransactionTestCase):
    def setUp(self):
        
        Publication.objects.all().delete()
        HarvestingEvent.objects.all().delete()

        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password="password"
        )
        self.source = Source.objects.create(
            url_field="https://example.com/oai?verb=ListRecords&metadataPrefix=oai_dc",
            collection_name="TestCollection",
            tags="test,harvest"
        )

    @patch("publications.tasks.requests.get")
    @patch("publications.tasks.parse_oai_xml_and_save_publications")
    def test_harvest_regular_metadata_sends_email(self, mock_parser, mock_get):
        fake_response = Mock()
        fake_response.raise_for_status = Mock()
        fake_response.content = b"<OAI-PMH><ListRecords></ListRecords></OAI-PMH>" 
        mock_get.return_value = fake_response

        def fake_parser(content, event, include_geometry):
            Publication.objects.create(
                title="Test Publication 1",
                doi="10.1000/1",
                job=event,
                timeperiod_startdate=[], 
                timeperiod_enddate=[]
            )
            if include_geometry:
                Publication.objects.create(
                    title="Test Publication 2",
                    doi="10.1000/2",
                    job=event,
                    geometry="dummy_geometry", 
                    timeperiod_startdate=[],
                    timeperiod_enddate=[]
                )
            else:
                Publication.objects.create(
                    title="Test Publication 2",
                    doi="10.1000/2",
                    job=event,
                    timeperiod_startdate=[],
                    timeperiod_enddate=[]
                )
        mock_parser.side_effect = fake_parser

        mail.outbox = []

        harvest_regular_metadata_from_source(self.source.id, self.user)

        event = HarvestingEvent.objects.filter(source=self.source).latest("started_at")

        new_count = Publication.objects.filter(job=event).count()
        self.assertEqual(new_count, 2, "Two publications should be created during harvest.")

        spatial_count = Publication.objects.filter(job=event).exclude(geometry__isnull=True).count()
        self.assertEqual(spatial_count, 0, "No publication should have spatial metadata if include_geometry is False.")

        temporal_count = Publication.objects.filter(job=event).exclude(timeperiod_startdate=[]).count()
        self.assertEqual(temporal_count, 0, "No publication has temporal metadata.")

        self.assertEqual(len(mail.outbox), 1, "One email should be sent to the user.")
        email = mail.outbox[0]
        self.assertIn("Harvesting Completed", email.subject)
        self.assertIn("Number of added articles: 2", email.body)    
        self.assertIn("Number of articles with spatial metadata: 0", email.body)
        self.assertIn("Number of articles with temporal metadata: 0", email.body)
        self.assertIn("TestCollection", email.body)
        self.assertIn(self.source.url_field, email.body)
        self.assertIn(event.started_at.strftime("%Y-%m-%d"), email.body)
