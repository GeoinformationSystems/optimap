# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Browser tests for the staff curation-tagger widget and "Run backfill now"
buttons on /countries and /regions.

works/static/js/curation-tagger.js drives an autosuggest combobox + removable
chips UX (typing to search, click/Enter to add, x to remove, then Assign),
shared between the country and region curation cards. tests/test_country_curation.py
and tests/test_region_curation.py already cover the underlying endpoints with
Django's test client, but that never executes the JS — these tests drive a
real browser to exercise the actual typing/click/chip/AJAX flow.
"""

from importlib import import_module

from django.conf import settings
from django.contrib.auth import (
    BACKEND_SESSION_KEY,
    HASH_SESSION_KEY,
    SESSION_KEY,
    get_user_model,
)
from django.contrib.gis.geos import GeometryCollection, MultiPolygon, Point, Polygon
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from helium import get_driver, kill_browser, start_chrome, wait_until
from selenium.webdriver.common.by import By

from works.models import Country, GlobalRegion, Work

User = get_user_model()


def _box(minx, miny, maxx, maxy):
    return MultiPolygon(Polygon(((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny))))


class _StaffBrowserTestCase(StaticLiveServerTestCase):
    def _login_in_browser(self, driver):
        """Inject a session cookie so the live-server browser is authenticated as staff."""
        engine = import_module(settings.SESSION_ENGINE)
        session = engine.SessionStore()
        session[SESSION_KEY] = str(self.staff.pk)
        session[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
        session[HASH_SESSION_KEY] = self.staff.get_session_auth_hash()
        session.save()
        driver.add_cookie({"name": settings.SESSION_COOKIE_NAME, "value": session.session_key, "path": "/"})

    def _start_as_staff(self, path):
        start_chrome(f"{self.live_server_url}/", headless=True)
        driver = get_driver()
        self._login_in_browser(driver)
        driver.get(f"{self.live_server_url}{path}")
        return driver

    def _click(self, driver, element):
        # The site footer is fixed-bottom and can overlap low-page elements in
        # the small headless viewport (ElementClickInterceptedException), so
        # every click in these tests goes through this scroll-first helper.
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        element.click()


class CountryCurationTaggerTests(_StaffBrowserTestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(username="admin", email="admin@example.com", password="pw")
        Country.objects.create(name="Germany", iso_code="DE", geom=_box(5, 47, 10, 55))
        # Geometry outside every country box, so it stays unmatched.
        self.work = Work.objects.create(status="p", title="Unmatched work", geometry=GeometryCollection(Point(-30, 0)))

    def test_search_add_chip_and_assign(self):
        try:
            driver = self._start_as_staff("/countries/")

            # Expand the collapsed curation card.
            toggle = driver.find_element(By.CSS_SELECTOR, '[data-target="#countryCurationBody"]')
            self._click(driver, toggle)
            wait_until(lambda: "show" in driver.find_element(By.ID, "countryCurationBody").get_attribute("class"))

            search = driver.find_element(By.CSS_SELECTOR, f"#country-tagger-{self.work.id} .curation-search")
            search.send_keys("Germ")

            wait_until(
                lambda: (
                    len(driver.find_elements(By.CSS_SELECTOR, f"#country-tagger-{self.work.id} .bok-suggestion")) > 0
                )
            )
            suggestion = driver.find_element(By.CSS_SELECTOR, f"#country-tagger-{self.work.id} .bok-suggestion")
            self.assertIn("Germany", suggestion.text)
            self._click(driver, suggestion)

            chips = driver.find_elements(By.CSS_SELECTOR, f"#country-tagger-{self.work.id} .curation-selected li")
            self.assertEqual(len(chips), 1)
            self.assertIn("Germany", chips[0].text)

            row = driver.find_element(By.ID, f"curation-row-{self.work.id}")
            self._click(driver, row.find_element(By.CSS_SELECTOR, ".curation-assign-btn"))

            # A successful Assign removes the row from the unmatched list.
            wait_until(lambda: len(driver.find_elements(By.ID, f"curation-row-{self.work.id}")) == 0)

            self.work.refresh_from_db()
            self.assertEqual(self.work.country_codes, ["DE"])

            get_driver().save_screenshot("tests-ui/screenshots/country_curation_tagger.png")
        finally:
            kill_browser()

    def test_remove_chip_before_assigning(self):
        try:
            driver = self._start_as_staff("/countries/")
            self._click(driver, driver.find_element(By.CSS_SELECTOR, '[data-target="#countryCurationBody"]'))
            wait_until(lambda: "show" in driver.find_element(By.ID, "countryCurationBody").get_attribute("class"))

            search = driver.find_element(By.CSS_SELECTOR, f"#country-tagger-{self.work.id} .curation-search")
            search.send_keys("Germ")
            wait_until(
                lambda: (
                    len(driver.find_elements(By.CSS_SELECTOR, f"#country-tagger-{self.work.id} .bok-suggestion")) > 0
                )
            )
            self._click(
                driver, driver.find_element(By.CSS_SELECTOR, f"#country-tagger-{self.work.id} .bok-suggestion")
            )
            self.assertEqual(
                len(driver.find_elements(By.CSS_SELECTOR, f"#country-tagger-{self.work.id} .curation-selected li")),
                1,
            )

            self._click(
                driver, driver.find_element(By.CSS_SELECTOR, f"#country-tagger-{self.work.id} .bok-chip-remove")
            )
            self.assertEqual(
                len(driver.find_elements(By.CSS_SELECTOR, f"#country-tagger-{self.work.id} .curation-selected li")),
                0,
            )
        finally:
            kill_browser()

    def test_backfill_button_shows_scheduled_status(self):
        try:
            driver = self._start_as_staff("/countries/")
            self._click(driver, driver.find_element(By.CSS_SELECTOR, '[data-target="#countryCurationBody"]'))
            wait_until(lambda: "show" in driver.find_element(By.ID, "countryCurationBody").get_attribute("class"))

            button = driver.find_element(By.ID, "countryBackfillBtn")
            self.assertTrue(button.get_attribute("data-csrf"), "button must carry a CSRF token")
            self._click(driver, button)

            status = driver.find_element(By.ID, "countryBackfillStatus")
            wait_until(lambda: status.text.strip() != "", timeout_secs=10)

            self.assertIn("scheduled", status.text.lower(), f"unexpected status text: {status.text!r}")
            self.assertIn("text-success", status.get_attribute("class"))
        finally:
            kill_browser()


class RegionCurationTaggerTests(_StaffBrowserTestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(username="admin", email="admin@example.com", password="pw")
        GlobalRegion.objects.create(
            name="Testland",
            region_type=GlobalRegion.CONTINENT,
            source_url="https://example.org/r",
            license="CC0",
            geom=_box(5, 47, 10, 55),
        )
        self.work = Work.objects.create(status="p", title="Unmatched work", geometry=GeometryCollection(Point(-30, 0)))

    def test_search_add_chip_and_assign(self):
        try:
            driver = self._start_as_staff("/regions/")

            toggle = driver.find_element(By.CSS_SELECTOR, '[data-target="#regionCurationBody"]')
            self._click(driver, toggle)
            wait_until(lambda: "show" in driver.find_element(By.ID, "regionCurationBody").get_attribute("class"))

            search = driver.find_element(By.CSS_SELECTOR, f"#region-tagger-{self.work.id} .curation-search")
            search.send_keys("Testland")

            wait_until(
                lambda: (
                    len(driver.find_elements(By.CSS_SELECTOR, f"#region-tagger-{self.work.id} .bok-suggestion")) > 0
                )
            )
            suggestion = driver.find_element(By.CSS_SELECTOR, f"#region-tagger-{self.work.id} .bok-suggestion")
            self.assertIn("Testland", suggestion.text)
            self._click(driver, suggestion)

            chips = driver.find_elements(By.CSS_SELECTOR, f"#region-tagger-{self.work.id} .curation-selected li")
            self.assertEqual(len(chips), 1)
            self.assertIn("Testland", chips[0].text)

            row = driver.find_element(By.ID, f"region-curation-row-{self.work.id}")
            self._click(driver, row.find_element(By.CSS_SELECTOR, ".curation-assign-btn"))

            wait_until(lambda: len(driver.find_elements(By.ID, f"region-curation-row-{self.work.id}")) == 0)

            self.work.refresh_from_db()
            self.assertEqual(list(self.work.regions.values_list("name", flat=True)), ["Testland"])
        finally:
            kill_browser()

    def test_backfill_button_shows_scheduled_status(self):
        try:
            driver = self._start_as_staff("/regions/")
            self._click(driver, driver.find_element(By.CSS_SELECTOR, '[data-target="#regionCurationBody"]'))
            wait_until(lambda: "show" in driver.find_element(By.ID, "regionCurationBody").get_attribute("class"))

            button = driver.find_element(By.ID, "regionBackfillBtn")
            self.assertTrue(button.get_attribute("data-csrf"), "button must carry a CSRF token")
            self._click(driver, button)

            status = driver.find_element(By.ID, "regionBackfillStatus")
            wait_until(lambda: status.text.strip() != "", timeout_secs=10)

            self.assertIn("scheduled", status.text.lower(), f"unexpected status text: {status.text!r}")
            self.assertIn("text-success", status.get_attribute("class"))
        finally:
            kill_browser()
