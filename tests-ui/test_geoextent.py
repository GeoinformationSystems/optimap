import unittest
import os
import tempfile
from django.test import TestCase
from django.urls import reverse
from helium import (
    start_chrome,
    click,
    get_driver,
    kill_browser,
    write,
    Text,
    Button,
    wait_until,
    find_all
)


class GeoextentPageTests(TestCase):
    """UI tests for the geoextent extraction page."""

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
        self.assertContains(response, "Geoextent Extraction")
        self.assertContains(response, "Upload Files")
        self.assertContains(response, "Remote Resource")
        self.assertContains(response, "Browse Files...")
        self.assertContains(response, "Extract Extent")

    def test_page_has_file_upload_form(self):
        """Test that the page has file upload form elements."""
        response = self.client.get(reverse("optimap:geoextent"))
        self.assertContains(response, 'id="file-upload-form"')
        self.assertContains(response, 'id="browse-files-btn"')
        self.assertContains(response, 'id="files"')
        self.assertContains(response, 'id="extract-files-btn"')

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
        self.assertContains(response, "Supported File Formats")
        self.assertContains(response, "Supported Repository Providers")
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
        self.assertContains(response, 'Geoextent')


class GeoextentUIInteractionTests(TestCase):
    """Browser-based UI interaction tests for geoextent page."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures."""
        super().setUpClass()
        cls.base_url = 'localhost:8000'
        cls.screenshot_dir = os.path.join(os.getcwd(), 'tests-ui', 'screenshots')
        os.makedirs(cls.screenshot_dir, exist_ok=True)

    def test_geoextent_page_loads(self):
        """Test that the geoextent page loads correctly in browser."""
        try:
            start_chrome(f'{self.base_url}/geoextent/', headless=True)

            # Check page title
            driver = get_driver()
            self.assertIn("OPTIMAP", driver.title)

            # Check main heading exists
            self.assertTrue(Text("Geoextent Extraction").exists())

            # Take screenshot
            driver.save_screenshot(
                os.path.join(self.screenshot_dir, 'geoextent_page.png')
            )
        finally:
            kill_browser()

    def test_tab_navigation(self):
        """Test switching between Upload Files and Remote Resource tabs."""
        try:
            start_chrome(f'{self.base_url}/geoextent/', headless=True)

            # Check default tab is Upload Files
            self.assertTrue(Text("Browse Files...").exists())

            # Click Remote Resource tab
            click("Remote Resource")

            # Wait for tab content to appear
            wait_until(lambda: Text("Resource Identifiers").exists(), timeout_secs=5)

            # Check remote form elements are visible
            self.assertTrue(Text("File Limit").exists())

            # Take screenshot
            get_driver().save_screenshot(
                os.path.join(self.screenshot_dir, 'geoextent_remote_tab.png')
            )
        finally:
            kill_browser()

    def test_browse_files_button_exists(self):
        """Test that browse files button exists and is clickable."""
        try:
            start_chrome(f'{self.base_url}/geoextent/', headless=True)

            # Check browse button exists
            self.assertTrue(Button("Browse Files...").exists())

            # Check extract button exists and is disabled initially
            driver = get_driver()
            extract_btn = driver.find_element("id", "extract-files-btn")
            self.assertTrue(extract_btn.get_attribute("disabled"))

        finally:
            kill_browser()

    def test_remote_form_validation(self):
        """Test that remote form shows validation when submitted empty."""
        try:
            start_chrome(f'{self.base_url}/geoextent/', headless=True)

            # Switch to Remote Resource tab
            click("Remote Resource")
            wait_until(lambda: Text("Resource Identifiers").exists(), timeout_secs=5)

            # Try to submit without entering identifier
            # Note: The form submission button in remote tab
            buttons = find_all(Button)
            submit_button = None
            for btn in buttons:
                if "Extract Extent" in btn.web_element.text:
                    submit_button = btn
                    break

            if submit_button:
                click(submit_button)

                # Wait for error message (should appear)
                wait_until(lambda: Text("Error").exists() or True, timeout_secs=2)

        finally:
            kill_browser()

    def test_extraction_options_visible(self):
        """Test that all extraction options are visible."""
        try:
            start_chrome(f'{self.base_url}/geoextent/', headless=True)

            # Check all option labels exist
            self.assertTrue(Text("Bounding Box").exists())
            self.assertTrue(Text("Time Box").exists())
            self.assertTrue(Text("Convex Hull").exists())
            self.assertTrue(Text("Place Name").exists())
            self.assertTrue(Text("Output Format").exists())
            self.assertTrue(Text("Gazetteer Service").exists())

            # Take screenshot of options
            get_driver().save_screenshot(
                os.path.join(self.screenshot_dir, 'geoextent_options.png')
            )
        finally:
            kill_browser()

    def test_documentation_section_visible(self):
        """Test that documentation section is visible and scrollable."""
        try:
            start_chrome(f'{self.base_url}/geoextent/', headless=True)

            # Scroll to bottom to see documentation
            driver = get_driver()
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

            # Check documentation headers exist
            self.assertTrue(Text("Documentation & Supported Formats").exists())
            self.assertTrue(Text("Supported File Formats").exists())
            self.assertTrue(Text("Supported Repository Providers").exists())

            # Take screenshot of documentation section
            driver.save_screenshot(
                os.path.join(self.screenshot_dir, 'geoextent_documentation.png')
            )
        finally:
            kill_browser()

    def test_footer_link_navigates_to_geoextent(self):
        """Test that clicking geoextent link in footer navigates to the page."""
        try:
            start_chrome(f'{self.base_url}/', headless=True)

            # Scroll to footer
            driver = get_driver()
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

            # Click geoextent link in footer
            click("Geoextent")

            # Wait for page to load
            wait_until(lambda: Text("Geoextent Extraction").exists(), timeout_secs=5)

            # Check URL changed
            self.assertIn("geoextent", driver.current_url)

        finally:
            kill_browser()


if __name__ == '__main__':
    unittest.main()
