# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import time
import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from helium import *


class MainpageTest(StaticLiveServerTestCase):
    @unittest.skip(
        "Timeline feature is disabled: works/templates/main.html has both the "
        "timeline_snippet.html include and js/timeline.js commented out, so no "
        "'Timeline' button exists on the page to click."
    )
    def test_timeline_navigation(self):
        """Test timeline button and visualization."""
        start_chrome(f"{self.live_server_url}/", headless=True)
        try:
            click(Button("Timeline"))
            time.sleep(2)
            get_driver().save_screenshot(os.path.join(os.getcwd(), "tests-ui", "screenshots", "Timeline.png"))
            time.sleep(2)
            if Text("Timeline Visualisation").exists():
                click(Link("The First Article-2010-10-10"))
            time.sleep(2)
            click(Button("Timeline"))
            time.sleep(2)
        finally:
            kill_browser()
