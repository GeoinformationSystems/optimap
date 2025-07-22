import django
import os

from django.test import TestCase, override_settings
from django.core import mail
from publications.tasks import send_monthly_email
from publications.models import EmailLog, Publication, UserProfile
from django.utils.timezone import now
from datetime import timedelta
from django.contrib.gis.geos import Point, GeometryCollection
from django.contrib.auth import get_user_model
User = get_user_model()

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class EmailIntegrationTest(TestCase):
    def setUp(self):
        """Setup test data before each test"""
        Publication.objects.all().delete()
        EmailLog.objects.all().delete()
        User.objects.all().delete()

        self.user = User.objects.create_user(
            username="testuser1", email="test@example.com", password="testpass"
        )
        self.user_profile = UserProfile.objects.get(user=self.user)
        self.user_profile.notify_new_manuscripts = True
        self.user_profile.save()

    def test_send_monthly_email_with_publications(self):
        """Test if the monthly email is sent when publications exist"""
        # create one publication with a DOI
        last_month = now().replace(day=1) - timedelta(days=1)
        publication = Publication.objects.create(
            title="Point Test",
            abstract="Publication with a single point inside a collection.",
            url="https://example.com/point",
            status="p",
            publicationDate=last_month,
            doi="10.1234/test-doi-1",
            geometry=GeometryCollection(Point(12.4924, 41.8902)),
        )
        # ensure creationDate falls in last month
        Publication.objects.filter(id=publication.id).update(creationDate=last_month)
        publication.refresh_from_db()

        # no emails before sending
        self.assertEqual(len(mail.outbox), 0)

        # send and assert
        send_monthly_email(sent_by=self.user)
        self.assertEqual(len(mail.outbox), 1)
        sent_email = mail.outbox[0]

        # title and DOI-based link should both appear
        self.assertIn(publication.title, sent_email.body)
        expected_link = f"https://doi.org/{publication.doi}"
        self.assertIn(expected_link, sent_email.body)

        # recipient and log correctness
        self.assertEqual(sent_email.to, ["test@example.com"])
        email_log = EmailLog.objects.latest('sent_at')
        self.assertEqual(email_log.recipient_email, "test@example.com")
        self.assertEqual(email_log.sent_by, self.user)

    def test_send_monthly_email_fallback_to_url_when_no_doi(self):
        """Test monthly email falls back to publication.url when no DOI"""
        last_month = now().replace(day=1) - timedelta(days=1)
        pub = Publication.objects.create(
            title="No DOI Paper",
            abstract="No DOI here.",
            url="https://example.com/nodoi",
            status="p",
            publicationDate=last_month,
            doi=None,
            geometry=GeometryCollection(Point(0, 0)),
        )
        Publication.objects.filter(id=pub.id).update(creationDate=last_month)
        mail.outbox.clear()

        send_monthly_email(sent_by=self.user)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body

        # should include URL fallback instead of DOI
        self.assertIn(pub.title, body)
        self.assertIn(pub.url, body)
