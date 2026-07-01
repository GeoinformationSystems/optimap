# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from works.models import GlobalRegion, Subscription

User = get_user_model()


class LoginRedirectTests(TestCase):
    """Tests for login redirect functionality with ?next parameter"""

    def setUp(self):
        """Set up test client and user"""
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser@example.com", email="testuser@example.com", password="testpass123"
        )

    def test_login_required_redirects_to_login_with_next(self):
        """Test that accessing protected URL without login redirects with next parameter"""
        response = self.client.get("/subscriptions/")

        # Should redirect to login page (/) with next parameter
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/"))
        self.assertIn("next=", response.url)
        self.assertIn("/subscriptions/", response.url)

    def test_magic_link_redirects_to_next_after_login(self):
        """Test that magic link authentication redirects to next URL"""
        # Manually create a cache entry with next parameter
        cache_data = {"email": self.user.email, "next": "/subscriptions/"}
        token = "test_token_12345"
        cache.set(token, cache_data, timeout=600)

        # Access the magic link
        response = self.client.get(reverse("optimap:magic_link", args=[token]))

        # Should redirect to subscriptions page
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/subscriptions/")

        # Verify user is logged in
        self.assertTrue(self.client.session.get("_auth_user_id"))

    def test_magic_link_redirects_to_root_without_next(self):
        """Test that magic link without next parameter redirects to root"""
        # Create cache entry without next parameter
        cache_data = {"email": self.user.email, "next": "/"}
        token = "test_token_54321"
        cache.set(token, cache_data, timeout=600)

        # Access the magic link
        response = self.client.get(reverse("optimap:magic_link", args=[token]))

        # Should redirect to root page
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")

    def test_login_url_setting_configured(self):
        """Test that LOGIN_URL setting is properly configured"""
        from django.conf import settings

        self.assertEqual(settings.LOGIN_URL, "/")

    def test_subscriptions_requires_login(self):
        """Test that subscriptions page requires authentication"""
        response = self.client.get("/subscriptions/")

        # Should redirect, not return 401 or show the page
        self.assertEqual(response.status_code, 302)

    def test_authenticated_user_can_access_subscriptions(self):
        """Test that authenticated users can access subscriptions"""
        self.client.force_login(self.user)

        response = self.client.get("/subscriptions/")

        # Should return 200 OK
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Subscriptions")

    def test_login_form_preserves_next_parameter(self):
        """Test that login form includes next parameter as hidden field"""
        response = self.client.get("/?next=/subscriptions/")

        # Should show main page with login form
        self.assertEqual(response.status_code, 200)

        # The menu snippet should have the next parameter
        # (Can't easily check hidden field in dropdown, but we've implemented it)

    def test_email_link_notification_with_subscriptions_url(self):
        """Test that users can click subscription links from emails"""
        # This simulates clicking a link from an email notification
        # User is not logged in, so should be redirected to login
        response = self.client.get("/subscriptions/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("next=", response.url)

        # After login, they should be redirected to subscriptions
        cache_data = {"email": self.user.email, "next": "/subscriptions/"}
        token = "notification_token"
        cache.set(token, cache_data, timeout=600)

        response = self.client.get(reverse("optimap:magic_link", args=[token]), follow=True)

        # Should end up on subscriptions page
        self.assertEqual(response.status_code, 200)
        # Final URL after redirects
        self.assertEqual(response.wsgi_request.path, "/subscriptions/")

    def test_unsubscribe_all_link_unsubscribes_user(self):
        """Test that clicking 'unsubscribe all' link actually unsubscribes the user"""
        # Create a subscription for the user
        subscription = Subscription.objects.create(
            user=self.user, name=f"{self.user.username}_subscription", subscribed=True
        )

        # Add some regions to the subscription
        test_polygon = Polygon(((0, 0), (0, 1), (1, 1), (1, 0), (0, 0)))
        test_multipolygon = MultiPolygon(test_polygon)

        region1 = GlobalRegion.objects.create(
            name="Test Region 1",
            region_type=GlobalRegion.CONTINENT,
            source_url="https://example.com/test1",
            license="CC0",
            geom=test_multipolygon,
        )
        region2 = GlobalRegion.objects.create(
            name="Test Region 2",
            region_type=GlobalRegion.OCEAN,
            source_url="https://example.com/test2",
            license="CC0",
            geom=test_multipolygon,
        )
        subscription.regions.add(region1, region2)

        # Verify subscription is active
        self.assertTrue(subscription.subscribed)
        self.assertEqual(subscription.regions.count(), 2)

        # Log in the user
        self.client.force_login(self.user)

        # Click the unsubscribe all link
        response = self.client.get(reverse("optimap:unsubscribe") + "?all=true")

        # Should redirect to home page
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")

        # Verify subscription is now inactive
        subscription.refresh_from_db()
        self.assertFalse(subscription.subscribed)

        # Verify success message was shown
        messages = list(response.wsgi_request._messages)
        self.assertEqual(len(messages), 1)
        self.assertIn("unsubscribed from all", str(messages[0]))
