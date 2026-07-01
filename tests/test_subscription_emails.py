# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import GeometryCollection, MultiPolygon, Point, Polygon
from django.test import TestCase
from django.utils import timezone

from tests.region_linking import link_all_work_regions
from works.models import Collection, EmailLog, GlobalRegion, Source, Subscription, Work
from works.tasks import send_subscription_based_email

User = get_user_model()


class SubscriptionEmailTests(TestCase):
    """Tests for regional subscription email notifications.

    Heavy fixture is built once per class via ``setUpTestData`` (Django 5.x
    deepcopies the instances per test, so tests can mutate ``self.user`` etc.
    without polluting siblings) — saves ~80% of the per-test wall-clock that
    the previous per-test ``setUp`` was eating.
    """

    def _send(self, **kwargs):
        # Region→work matching now reads the persisted Work.regions M2M; populate
        # it (as the production signal/sweep would) before exercising the digest.
        link_all_work_regions()
        return send_subscription_based_email(**kwargs)

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )
        cls.source = Source.objects.create(
            name="Test Journal",
            url_field="https://example.com/test",
            issn_l="1234-5678",
        )
        # Africa region (simple polygon around coordinates 0,0 to 10,10)
        africa_polygon = Polygon(((0, 0), (0, 10), (10, 10), (10, 0), (0, 0)))
        cls.africa = GlobalRegion.objects.create(
            name="Africa",
            region_type=GlobalRegion.CONTINENT,
            source_url="https://example.com/africa",
            license="CC0",
            geom=MultiPolygon(africa_polygon),
        )
        # Asia region (simple polygon around coordinates 20,20 to 30,30)
        asia_polygon = Polygon(((20, 20), (20, 30), (30, 30), (30, 20), (20, 20)))
        cls.asia = GlobalRegion.objects.create(
            name="Asia",
            region_type=GlobalRegion.CONTINENT,
            source_url="https://example.com/asia",
            license="CC0",
            geom=MultiPolygon(asia_polygon),
        )
        # Pacific Ocean region (simple polygon around coordinates 40,40 to 50,50)
        pacific_polygon = Polygon(((40, 40), (40, 50), (50, 50), (50, 40), (40, 40)))
        cls.pacific = GlobalRegion.objects.create(
            name="Pacific Ocean",
            region_type=GlobalRegion.OCEAN,
            source_url="https://example.com/pacific",
            license="CC0",
            geom=MultiPolygon(pacific_polygon),
        )
        cls.pub_africa = Work.objects.create(
            title="African Study on Climate Change",
            doi="10.1234/africa.2024",
            status="p",
            source=cls.source,
            geometry=GeometryCollection(Point(5, 5)),
        )
        cls.pub_asia = Work.objects.create(
            title="Asian Biodiversity Research",
            doi="10.1234/asia.2024",
            status="p",
            source=cls.source,
            geometry=GeometryCollection(Point(25, 25)),
        )
        cls.pub_pacific = Work.objects.create(
            title="Pacific Ocean Current Patterns",
            doi="10.1234/pacific.2024",
            status="p",
            source=cls.source,
            geometry=GeometryCollection(Point(45, 45)),
        )
        cls.collection = Collection.objects.create(
            identifier="test-collection",
            name="Test Collection",
            is_published=True,
        )
        cls.pub_collection = Work.objects.create(
            title="Collected Work on Wetlands",
            doi="10.1234/collection.2024",
            status="p",
            source=cls.source,
        )
        cls.pub_collection.collections.add(cls.collection)

    def test_globalregion_get_slug(self):
        """Test that GlobalRegion generates correct slugs"""
        self.assertEqual(self.africa.get_slug(), "africa")
        self.assertEqual(self.pacific.get_slug(), "pacific-ocean")

    def test_globalregion_get_absolute_url(self):
        """Test that GlobalRegion generates correct URLs"""
        africa_url = self.africa.get_absolute_url()
        self.assertIn("/regions/continent/africa/", africa_url)

        pacific_url = self.pacific.get_absolute_url()
        self.assertIn("/regions/ocean/pacific-ocean/", pacific_url)

    def test_globalregion_str_representation(self):
        """Test the __str__ method of GlobalRegion"""
        self.assertEqual(str(self.africa), "Africa (Continent)")
        self.assertEqual(str(self.pacific), "Pacific Ocean (Ocean)")

    @patch("works.tasks.EmailMessage")
    def test_email_sent_for_subscribed_regions(self, mock_email):
        """Test that emails are sent when publications match subscribed regions"""
        # Create subscription for Africa
        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.regions.add(self.africa)

        # Send emails
        self._send(trigger_source="test")

        # Verify email was sent
        self.assertTrue(mock_email.called)
        call_args = mock_email.call_args

        # Check subject
        subject = call_args[0][0]
        self.assertIn("new publications", subject)

        # Check content includes region name and publication
        content = call_args[0][1]
        self.assertIn("Africa", content)
        self.assertIn("African Study on Climate Change", content)

    @patch("works.tasks.EmailMessage")
    def test_email_grouped_by_region(self, mock_email):
        """Test that email content groups publications by region"""
        # Create subscription for multiple regions
        subscription = Subscription.objects.create(user=self.user, name="multi_region_subscription", subscribed=True)
        subscription.regions.add(self.africa, self.asia)

        # Send emails
        self._send(trigger_source="test")

        # Verify email structure
        self.assertTrue(mock_email.called)
        content = mock_email.call_args[0][1]

        # Check that both regions appear
        self.assertIn("Africa", content)
        self.assertIn("Asia", content)

        # Check that publications are grouped correctly
        self.assertIn("African Study on Climate Change", content)
        self.assertIn("Asian Biodiversity Research", content)

        # Pacific publication should NOT be included
        self.assertNotIn("Pacific Ocean Current Patterns", content)

    @patch("works.tasks.EmailMessage")
    def test_email_includes_region_landing_page_links(self, mock_email):
        """Test that email includes links to region landing pages"""
        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.regions.add(self.africa)

        self._send(trigger_source="test")

        content = mock_email.call_args[0][1]

        # Check for region landing page link
        self.assertIn("View all publications in this region", content)
        self.assertIn("/regions/continent/africa/", content)

    @patch("works.tasks.EmailMessage")
    def test_no_email_sent_when_no_publications(self, mock_email):
        """Test that no email is sent when there are no matching publications"""
        # Create subscription for Pacific (which has a publication)
        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.regions.add(self.pacific)

        # Delete the Pacific publication
        self.pub_pacific.delete()

        # Send emails
        self._send(trigger_source="test")

        # Verify no email was sent
        self.assertFalse(mock_email.called)

    @patch("works.tasks.EmailMessage")
    def test_no_email_sent_when_no_regions_selected(self, mock_email):
        """Test that no email is sent when user has no regions selected"""
        # Create subscription without regions
        Subscription.objects.create(user=self.user, name="empty_subscription", subscribed=True)

        # Send emails
        self._send(trigger_source="test")

        # Verify no email was sent
        self.assertFalse(mock_email.called)

    @patch("works.tasks.EmailMessage")
    def test_email_includes_manage_subscriptions_link(self, mock_email):
        """Test that email includes link to manage subscriptions"""
        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.regions.add(self.africa)

        self._send(trigger_source="test")

        content = mock_email.call_args[0][1]

        # Check for management links
        self.assertIn("Manage your subscriptions", content)
        self.assertIn("/subscriptions", content)
        self.assertIn("Unsubscribe from all notifications", content)

    @patch("works.tasks.EmailMessage")
    def test_email_shows_correct_publication_count(self, mock_email):
        """Test that email shows correct count of publications per region"""
        # Add a second publication to Africa
        africa_point2 = Point(7, 7)
        Work.objects.create(
            title="Another African Study",
            doi="10.1234/africa2.2024",
            status="p",
            source=self.source,
            geometry=GeometryCollection(africa_point2),
        )

        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.regions.add(self.africa)

        self._send(trigger_source="test")

        content = mock_email.call_args[0][1]

        # Check count in subject
        subject = mock_email.call_args[0][0]
        self.assertIn("2 new publications", subject)

        # Check count per region
        self.assertIn("Africa (Continent) - 2 work(s)", content)

    @patch("works.tasks.EmailMessage")
    def test_only_published_works_included(self, mock_email):
        """Test that only published works are included in notifications"""
        # Create a draft publication in Africa
        africa_point_draft = Point(6, 6)
        Work.objects.create(
            title="Draft African Study",
            doi="10.1234/africa_draft.2024",
            status="d",  # Draft
            source=self.source,
            geometry=GeometryCollection(africa_point_draft),
        )

        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.regions.add(self.africa)

        self._send(trigger_source="test")

        content = mock_email.call_args[0][1]

        # Draft publication should not be included
        self.assertNotIn("Draft African Study", content)
        # Only the published one
        self.assertIn("African Study on Climate Change", content)

    @patch("works.tasks.EmailMessage")
    def test_email_limits_publications_per_region(self, mock_email):
        """Test that email limits the number of publications shown per region"""
        # Create 15 publications in Africa (more than the 10 per region limit)
        for i in range(15):
            point = Point(5 + i * 0.1, 5 + i * 0.1)
            Work.objects.create(
                title=f"African Study {i + 2}",
                doi=f"10.1234/africa{i + 2}.2024",
                status="p",
                source=self.source,
                geometry=GeometryCollection(point),
            )

        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.regions.add(self.africa)

        self._send(trigger_source="test")

        content = mock_email.call_args[0][1]

        # Should mention "and X more"
        self.assertIn("and", content)
        self.assertIn("more in Africa", content)

    def test_email_log_created_on_success(self):
        """Test that EmailLog entry is created when email is sent successfully"""
        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.regions.add(self.africa)

        with patch("works.tasks.EmailMessage") as mock_email:
            mock_instance = mock_email.return_value
            mock_instance.send.return_value = None

            self._send(trigger_source="test")

            # Check EmailLog was created
            log_entry = EmailLog.objects.filter(recipient_email=self.user.email).first()
            self.assertIsNotNone(log_entry)
            self.assertEqual(log_entry.status, "success")

    # ------------------------------------------------------------------
    # Interval filtering and last_notified tracking (issue #85)
    # ------------------------------------------------------------------

    @patch("works.tasks.EmailMessage")
    def test_weekly_interval_only_includes_recent_works(self, mock_email):
        """Weekly subscriptions only see works published in the last 7 days."""
        subscription = Subscription.objects.create(
            user=self.user,
            name="test_subscription",
            subscribed=True,
            notification_interval="weekly",
        )
        subscription.regions.add(self.africa)

        # Move pub_africa back to 10 days ago (outside weekly window)
        Work.objects.filter(pk=self.pub_africa.pk).update(creationDate=timezone.now() - timedelta(days=10))
        # Create a recent work (3 days ago, inside weekly window)
        recent = Work.objects.create(
            title="Very Recent African Study",
            doi="10.1234/africa_recent.2024",
            status="p",
            source=self.source,
            geometry=GeometryCollection(Point(5, 5)),
        )
        Work.objects.filter(pk=recent.pk).update(creationDate=timezone.now() - timedelta(days=3))

        self._send(trigger_source="test", interval="weekly")

        self.assertTrue(mock_email.called)
        content = mock_email.call_args[0][1]
        self.assertIn("Very Recent African Study", content)
        self.assertNotIn("African Study on Climate Change", content)

    @patch("works.tasks.EmailMessage")
    def test_monthly_interval_only_includes_recent_works(self, mock_email):
        """Monthly subscriptions only see works published in the last 31 days."""
        subscription = Subscription.objects.create(
            user=self.user,
            name="test_subscription",
            subscribed=True,
            notification_interval="monthly",
        )
        subscription.regions.add(self.africa)

        # Move pub_africa back to 40 days ago (outside monthly window)
        Work.objects.filter(pk=self.pub_africa.pk).update(creationDate=timezone.now() - timedelta(days=40))
        # Create a work 10 days ago (inside monthly window)
        mid = Work.objects.create(
            title="Mid-Month African Study",
            doi="10.1234/africa_mid.2024",
            status="p",
            source=self.source,
            geometry=GeometryCollection(Point(5, 5)),
        )
        Work.objects.filter(pk=mid.pk).update(creationDate=timezone.now() - timedelta(days=10))

        self._send(trigger_source="test", interval="monthly")

        self.assertTrue(mock_email.called)
        content = mock_email.call_args[0][1]
        self.assertIn("Mid-Month African Study", content)
        self.assertNotIn("African Study on Climate Change", content)

    @patch("works.tasks.EmailMessage")
    def test_interval_filter_skips_wrong_interval(self, mock_email):
        """Calling with interval='weekly' skips monthly subscribers and vice versa."""
        subscription = Subscription.objects.create(
            user=self.user,
            name="test_subscription",
            subscribed=True,
            notification_interval="monthly",
        )
        subscription.regions.add(self.africa)

        self._send(trigger_source="test", interval="weekly")

        self.assertFalse(mock_email.called)

    @patch("works.tasks.EmailMessage")
    def test_last_notified_updated_after_send(self, mock_email):
        """last_notified is set on the subscription after a successful send."""
        subscription = Subscription.objects.create(
            user=self.user,
            name="test_subscription",
            subscribed=True,
        )
        subscription.regions.add(self.africa)
        self.assertIsNone(subscription.last_notified)

        before = timezone.now()
        self._send(trigger_source="test")
        after = timezone.now()

        subscription.refresh_from_db()
        self.assertIsNotNone(subscription.last_notified)
        self.assertGreaterEqual(subscription.last_notified, before)
        self.assertLessEqual(subscription.last_notified, after)

    @patch("works.tasks.EmailMessage")
    def test_last_notified_not_updated_when_no_new_pubs(self, mock_email):
        """last_notified is NOT updated when there are no new publications."""
        subscription = Subscription.objects.create(
            user=self.user,
            name="test_subscription",
            subscribed=True,
        )
        subscription.regions.add(self.africa)

        # Push pub_africa far into the past so it falls outside the 31-day window
        Work.objects.filter(pk=self.pub_africa.pk).update(creationDate=timezone.now() - timedelta(days=60))

        self._send(trigger_source="test")

        self.assertFalse(mock_email.called)
        subscription.refresh_from_db()
        self.assertIsNone(subscription.last_notified)

    @patch("works.tasks.EmailMessage")
    def test_last_notified_used_as_cutoff(self, mock_email):
        """When last_notified is set, only works created after it are included."""
        five_days_ago = timezone.now() - timedelta(days=5)
        subscription = Subscription.objects.create(
            user=self.user,
            name="test_subscription",
            subscribed=True,
            last_notified=five_days_ago,
        )
        subscription.regions.add(self.africa)

        # pub_africa is recent (just created) — within 5-day window ✓
        # Create an old work (8 days ago) — before last_notified ✗
        old = Work.objects.create(
            title="Old African Study",
            doi="10.1234/africa_old.2024",
            status="p",
            source=self.source,
            geometry=GeometryCollection(Point(5, 5)),
        )
        Work.objects.filter(pk=old.pk).update(creationDate=timezone.now() - timedelta(days=8))

        self._send(trigger_source="test")

        self.assertTrue(mock_email.called)
        content = mock_email.call_args[0][1]
        self.assertIn("African Study on Climate Change", content)
        self.assertNotIn("Old African Study", content)

    # ------------------------------------------------------------------
    # Source / collection subscriptions (issue #80)
    # ------------------------------------------------------------------

    @patch("works.tasks.EmailMessage")
    def test_email_sent_for_subscribed_source(self, mock_email):
        """Test that emails are sent when publications match a subscribed source."""
        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.sources.add(self.source)

        self._send(trigger_source="test")

        self.assertTrue(mock_email.called)
        content = mock_email.call_args[0][1]
        self.assertIn("🗄️ Sources", content)
        self.assertIn("Test Journal", content)
        self.assertIn("African Study on Climate Change", content)

    @patch("works.tasks.EmailMessage")
    def test_email_sent_for_subscribed_collection(self, mock_email):
        """Test that emails are sent when publications match a subscribed collection."""
        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.collections.add(self.collection)

        self._send(trigger_source="test")

        self.assertTrue(mock_email.called)
        content = mock_email.call_args[0][1]
        self.assertIn("📚 Collections", content)
        self.assertIn("Test Collection", content)
        self.assertIn("Collected Work on Wetlands", content)

    @patch("works.tasks.EmailMessage")
    def test_no_source_or_collection_section_when_not_subscribed(self, mock_email):
        """Sections only render for the types the user actually subscribed to."""
        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.regions.add(self.africa)

        self._send(trigger_source="test")

        self.assertTrue(mock_email.called)
        content = mock_email.call_args[0][1]
        self.assertNotIn("🗄️ Sources", content)
        self.assertNotIn("📚 Collections", content)

    @patch("works.tasks.EmailMessage")
    def test_work_appears_in_both_region_and_source_sections(self, mock_email):
        """A work matching both a subscribed region and a subscribed source appears
        in both sections — sections are not deduplicated against each other."""
        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.regions.add(self.africa)
        subscription.sources.add(self.source)

        self._send(trigger_source="test")

        content = mock_email.call_args[0][1]
        self.assertEqual(content.count("African Study on Climate Change"), 2)

    def test_last_notified_advances_for_source_only_subscription(self):
        """last_notified is updated after a successful send driven purely by a
        subscribed source (no regions/collections)."""
        subscription = Subscription.objects.create(user=self.user, name="test_subscription", subscribed=True)
        subscription.sources.add(self.source)
        self.assertIsNone(subscription.last_notified)

        with patch("works.tasks.EmailMessage") as mock_email:
            mock_email.return_value.send.return_value = None
            self._send(trigger_source="test")

        subscription.refresh_from_db()
        self.assertIsNotNone(subscription.last_notified)
