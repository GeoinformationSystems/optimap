# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import os
from time import sleep

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from helium import *

User = get_user_model()


class LoginresponseTest(StaticLiveServerTestCase):
    def setUp(self):
        """Set up the test user and start browser"""
        self.email = "dev@example.com"
        self.token = "mock-login-token-12345"

        # Clean up any existing users with this email
        User.objects.filter(email=self.email).delete()

        # Create cache entry for login token (matching the structure in auth.py)
        cache.set(self.token, {"email": self.email, "next": "/"}, timeout=300)

        # Start browser
        self.browser = start_chrome(f"{self.live_server_url}/", headless=True)

    def test_login_response(self):
        """Test login flow and response message."""
        try:
            # Click unified menu dropdown
            click(S("#unifiedMenuDropdown"))

            # Fill in email and submit login form
            write(self.email, into="email")
            get_driver().save_screenshot(os.path.join(os.getcwd(), "tests-ui", "screenshots", "Loginmailid.png"))

            # Click the Send/Login button
            click(S('button[type="submit"]'))

            # Wait for response page to load
            sleep(2)
            get_driver().save_screenshot(os.path.join(os.getcwd(), "tests-ui", "screenshots", "Loginresponse.png"))

            # Verify success message appears
            self.assertTrue(Text("We sent a login link").exists(), "Login response message not found")

            # Now test the actual login using the token. GET only shows a
            # confirmation page (it must not authenticate on its own — see
            # the SessionInterrupted/link-prefetch regression this guards
            # against); the POST triggered by the button below performs the
            # actual login.
            go_to(f"{self.live_server_url}/login/{self.token}")
            sleep(3)

            if Button("I consent").exists():
                click(Button("I consent"))
            else:
                click(Button("Log in"))
            sleep(2)

            # Verify we're logged in by checking for user menu
            click(S("#unifiedMenuDropdown"))
            self.assertTrue(
                Text("My subscriptions").exists() or Text("Settings").exists(), "User menu items not found after login"
            )

        finally:
            kill_browser()

    def tearDown(self):
        """Clean up after test"""
        # Clean up test user
        User.objects.filter(email=self.email).delete()
        # Clean up cache
        cache.delete(self.token)
