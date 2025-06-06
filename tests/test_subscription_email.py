from django.test import TestCase, override_settings
from django.core import mail
from publications.models import Subscription, Publication, EmailLog, UserProfile
from publications.tasks import send_subscription_based_email
from django.contrib.auth import get_user_model
from django.utils.timezone import now
from datetime import timedelta
from django.contrib.gis.geos import Point, GeometryCollection

User = get_user_model()

@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class SubscriptionEmailTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="subuser", email="subuser@example.com", password="testpass")
        UserProfile.objects.get_or_create(user=self.user)

        self.subscription = Subscription.objects.create(
            user=self.user,
            name="Test Subscription",
            search_term="AI",
            region=GeometryCollection(Point(12.4924, 41.8902)), 
            subscribed=True
        )

    def test_subscription_email_sent_when_publication_matches(self):
        # Create a publication within the region
        pub = Publication.objects.create(
            title="Rome AI Paper",
            abstract="Test abstract",
            url="https://example.com/pub",
            status="p",
            publicationDate=now() - timedelta(days=5),
            doi="10.1234/sub-doi",
            geometry=GeometryCollection(Point(12.4924, 41.8902)),
        )

        send_subscription_based_email(sent_by=self.user)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(pub.title, mail.outbox[0].body)
        self.assertIn("unsubscribe", mail.outbox[0].body.lower())

        log = EmailLog.objects.latest("sent_at")
        self.assertEqual(log.recipient_email, self.user.email)
        self.assertEqual(log.sent_by, self.user)

    def test_subscription_email_not_sent_if_no_publication_matches(self):
        # Create publication OUTSIDE subscription region
        Publication.objects.create(
            title="Outside Region Paper",
            abstract="Should not match",
            url="https://example.com/outside",
            status="p",
            publicationDate=now(),
            doi="10.1234/outside-doi",
            geometry=GeometryCollection(Point(0, 0)),  # Outside region
        )

        send_subscription_based_email(sent_by=self.user)

        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(EmailLog.objects.exists())
