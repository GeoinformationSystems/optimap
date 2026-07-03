# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import caches
from django.test import TestCase
from django.urls import reverse
from helium import Button, Text, click, get_driver, kill_browser, start_chrome, wait_until


class GeoextentPageTests(TestCase):
    """UI tests for the geoextent extraction page."""

    def setUp(self):
        # /geoextent/ is @cache_page(cache="memory"); clear it so each test
        # sees a fresh render rather than a cached response from an earlier
        # test (assertTemplateUsed fails on a cache hit since nothing renders).
        caches["memory"].clear()

    def test_url_exists_at_correct_location(self):
        """Test that the geoextent URL returns 200."""
        response = self.client.get("/geoextent/")
        self.assertEqual(response.status_code, 200)

    def test_url_available_by_name(self):
        """Test that the geoextent URL is accessible by name."""
        response = self.client.get(reverse("optimap:geoextent"))
        self.assertEqual(response.status_code, 200)

    def test_template_name_correct(self):
        """Test that the correct template is used."""
        response = self.client.get(reverse("optimap:geoextent"))
        self.assertTemplateUsed(response, "geoextent.html")

    def test_template_content(self):
        """Test that the page contains expected content."""
        response = self.client.get(reverse("optimap:geoextent"))
        self.assertContains(response, "Geoextent extraction")
        self.assertContains(response, "Upload files")
        self.assertContains(response, "Remote resources")
        self.assertContains(response, "Browse files...")
        self.assertContains(response, "Add extent to map")

    def test_page_has_file_upload_form(self):
        """Test that the page has file upload form elements."""
        response = self.client.get(reverse("optimap:geoextent"))
        self.assertContains(response, 'id="file-upload-form"')
        self.assertContains(response, 'id="browse-files-btn"')
        self.assertContains(response, 'id="files"')
        self.assertContains(response, 'id="add-extent-btn"')

    def test_page_has_remote_resource_form(self):
        """Test that the page has remote resource form elements."""
        response = self.client.get(reverse("optimap:geoextent"))
        self.assertContains(response, 'id="remote-form"')
        self.assertContains(response, 'id="identifiers"')
        self.assertContains(response, 'id="file_limit"')
        self.assertContains(response, 'id="size_limit_mb"')

    def test_page_has_extraction_options(self):
        """Test that the page has all extraction option checkboxes."""
        response = self.client.get(reverse("optimap:geoextent"))
        self.assertContains(response, 'id="bbox"')
        self.assertContains(response, 'id="tbox"')
        self.assertContains(response, 'id="convex_hull"')
        self.assertContains(response, 'id="placename"')
        self.assertContains(response, 'id="response_format"')
        self.assertContains(response, 'id="gazetteer"')

    def test_page_has_documentation_section(self):
        """Test that the page has documentation section."""
        response = self.client.get(reverse("optimap:geoextent"))
        self.assertContains(response, "Supported file formats")
        self.assertContains(response, "Supported repository providers")
        self.assertContains(response, "geoextent")  # Should show version

    def test_page_displays_geoextent_version(self):
        """Test that the page displays the geoextent version."""
        response = self.client.get(reverse("optimap:geoextent"))
        # Should contain version information
        self.assertContains(response, "geoextent v")

    def test_page_has_map_container(self):
        """Test that the page has a map container."""
        response = self.client.get(reverse("optimap:geoextent"))
        self.assertContains(response, 'id="geoextent-map"')

    def test_footer_link_exists(self):
        """Test that geoextent link exists in footer."""
        response = self.client.get("/")
        self.assertContains(response, 'href="/geoextent/"')
        self.assertContains(response, "Geoextent")


class GeoextentUIInteractionTests(StaticLiveServerTestCase):
    """Browser-based UI interaction tests for geoextent page."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures."""
        super().setUpClass()
        cls.screenshot_dir = os.path.join(os.getcwd(), "tests-ui", "screenshots")
        os.makedirs(cls.screenshot_dir, exist_ok=True)

    def test_geoextent_page_loads(self):
        """Test that the geoextent page loads correctly in browser."""
        try:
            start_chrome(f"{self.live_server_url}/geoextent/", headless=True)

            # Check page title
            driver = get_driver()
            self.assertIn("OPTIMAP", driver.title)

            # Check main heading exists
            self.assertTrue(Text("Geoextent extraction").exists())

            # Take screenshot
            driver.save_screenshot(os.path.join(self.screenshot_dir, "geoextent_page.png"))
        finally:
            kill_browser()

    def test_tab_navigation(self):
        """Test switching between Upload Files and Remote Resource tabs."""
        try:
            start_chrome(f"{self.live_server_url}/geoextent/", headless=True)

            # Check default tab is Upload files
            self.assertTrue(Text("Browse files...").exists())

            # Click Remote resources tab
            click("Remote resources")

            # Wait for tab content to appear
            wait_until(lambda: Text("Resource Identifiers").exists(), timeout_secs=5)

            # Check remote form elements are visible
            self.assertTrue(Text("File Limit").exists())

            # Take screenshot
            get_driver().save_screenshot(os.path.join(self.screenshot_dir, "geoextent_remote_tab.png"))
        finally:
            kill_browser()

    def test_browse_files_button_exists(self):
        """Test that browse files button exists and is clickable."""
        try:
            start_chrome(f"{self.live_server_url}/geoextent/", headless=True)

            # Check browse button exists
            self.assertTrue(Button("Browse files...").exists())

            # Check extract button exists and is disabled initially
            driver = get_driver()
            extract_btn = driver.find_element("id", "add-extent-btn")
            self.assertTrue(extract_btn.get_attribute("disabled"))

        finally:
            kill_browser()

    def test_remote_form_validation(self):
        """Test that remote form shows validation when submitted empty."""
        try:
            start_chrome(f"{self.live_server_url}/geoextent/", headless=True)

            # Switch to Remote resources tab
            click("Remote resources")
            wait_until(lambda: Text("Resource identifiers").exists(), timeout_secs=5)

            # Try to submit without entering identifier
            # Try to submit - should show validation error from browser
            # The "Add extent to map" button should be disabled when no identifier is entered
            driver = get_driver()
            submit_btn = driver.find_element("id", "add-extent-btn")
            # Button should be disabled if no files/identifiers selected
            self.assertTrue(submit_btn.get_attribute("disabled"))

        finally:
            kill_browser()

    def test_extraction_options_visible(self):
        """Test that all extraction options are visible after clicking on the options link."""
        try:
            start_chrome(f"{self.live_server_url}/geoextent/", headless=True)

            self.assertFalse(Text("Bounding box").exists())
            self.assertFalse(Text("Bounding box").exists())
            self.assertFalse(Text("Time box").exists())
            self.assertFalse(Text("Convex hull").exists())
            self.assertFalse(Text("Place name").exists())
            self.assertFalse(Text("Output format").exists())
            self.assertFalse(Text("Gazetteer service").exists())

            # The site footer is fixed-bottom and can overlap this card header
            # in the small headless viewport, so click() sometimes hits the
            # footer instead — scroll the header to the middle of the
            # viewport first so the real click lands on it.
            driver = get_driver()
            header = driver.find_element("css selector", '[data-target="#extraction-options-body"]')
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", header)
            header.click()

            # Check all option labels exist
            wait_until(lambda: Text("Bounding box").exists(), timeout_secs=5)
            self.assertTrue(Text("Time box").exists())
            self.assertTrue(Text("Convex hull").exists())
            self.assertTrue(Text("Place name").exists())
            self.assertTrue(Text("Output format").exists())
            self.assertTrue(Text("Gazetteer service").exists())

            # Take screenshot of options
            get_driver().save_screenshot(os.path.join(self.screenshot_dir, "geoextent_options.png"))
        finally:
            kill_browser()

    def test_documentation_section_visible(self):
        """Test that documentation section is visible and scrollable."""
        try:
            start_chrome(f"{self.live_server_url}/geoextent/", headless=True)

            # Scroll to bottom to see documentation
            driver = get_driver()
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

            # Check documentation headers exist
            self.assertTrue(Text("Documentation & supported formats").exists())
            self.assertTrue(Text("Supported file formats").exists())
            self.assertTrue(Text("Supported repository providers").exists())

            # Take screenshot of documentation section
            driver.save_screenshot(os.path.join(self.screenshot_dir, "geoextent_documentation.png"))
        finally:
            kill_browser()

    def test_sitemap_link_navigates_to_geoextent(self):
        """Test that clicking geoextent link on the user sitemap navigates to the page."""
        try:
            start_chrome(f"{self.live_server_url}/pages/", headless=True)

            # The page also has a "Geoextent" entry in the (collapsed, hidden)
            # burger-menu dropdown from base.html — click("Geoextent") can
            # match that one first and no-op instead of navigating, so target
            # the visible sitemap link explicitly.
            driver = get_driver()
            sitemap_link = next(
                el for el in driver.find_elements("partial link text", "Geoextent") if el.is_displayed()
            )
            # The site footer is fixed-bottom and can overlap sitemap entries
            # low on the page in the small headless viewport.
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", sitemap_link)
            sitemap_link.click()

            # Wait for page to load
            wait_until(lambda: Text("Geoextent extraction").exists(), timeout_secs=5)

            # Check URL changed
            self.assertIn("geoextent", driver.current_url)

        finally:
            kill_browser()


if __name__ == "__main__":
    unittest.main()
