from django.test import TestCase
from django.core import mail
from publications.tasks import send_monthly_email
from publications.models import SentEmailLog, Publication
from django.utils.timezone import now
from django.contrib.auth.models import User

class EmailIntegrationTest(TestCase):
    def setUp(self):
        """Setup test data before each test"""
        self.user = User.objects.create_user(username="testuser", email="test@example.com", password="testpass")

    def test_send_monthly_email_with_publications(self):
        """Test if the monthly email is sent when publications exist"""
        Publication.objects.create(title="Test Manuscript", creationDate=now()) 
        self.assertEqual(len(mail.outbox), 0)
        send_monthly_email(sent_by=self.user)

        self.assertEqual(len(mail.outbox), 1)

        sent_email = mail.outbox[0]
        self.assertIn("New Manuscripts This Month", sent_email.subject)
        self.assertIn("Test Manuscript", sent_email.body)
        self.assertEqual(sent_email.to, ["test@example.com"])

        self.assertTrue(SentEmailLog.objects.filter(recipient_email="test@example.com").exists())

        email_log = SentEmailLog.objects.get(recipient_email="test@example.com")
        self.assertEqual(email_log.sent_by, self.user)

    def test_send_monthly_email_without_publications(self):
        """Test that no email is sent when no new publications exist"""
        self.assertEqual(len(mail.outbox), 0)

        send_monthly_email(sent_by=self.user) 

        self.assertEqual(len(mail.outbox), 0)

        self.assertFalse(SentEmailLog.objects.exists())
