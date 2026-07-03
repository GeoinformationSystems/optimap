# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import os

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import caches
from django.urls import reverse
from helium import click, get_driver, kill_browser, start_chrome


class PrivacypageTests(StaticLiveServerTestCase):
    def setUp(self):
        # /privacy/ is @cache_page(cache="memory"); clear it so each test sees
        # a fresh render rather than a cached response from an earlier test
        # (assertTemplateUsed fails on a cache hit since nothing is rendered).
        caches["memory"].clear()

    def test_privacy_link(self):
        start_chrome(f"{self.live_server_url}/", headless=True)
        click("privacy")
        get_driver().save_screenshot(os.path.join(os.getcwd(), "tests-ui", "screenshots", "privacy.png"))
        kill_browser()

    def test_url_exists_at_correct_location(self):
        response = self.client.get("/privacy/")
        self.assertEqual(response.status_code, 200)

    def test_url_available_by_name(self):
        response = self.client.get(reverse("optimap:privacy"))
        self.assertEqual(response.status_code, 200)

    def test_template_name_correct(self):
        response = self.client.get(reverse("optimap:privacy"))
        self.assertTemplateUsed(response, "privacy.html")

    def test_template_content(self):
        response = self.client.get(reverse("optimap:privacy"))
        self.assertContains(response, "Privacy policy")
