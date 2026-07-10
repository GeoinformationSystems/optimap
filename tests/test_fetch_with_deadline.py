# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for works/harvesting/sessions.py::fetch_with_deadline — a wall-clock
ceiling for HTTP fetches, on top of requests' own per-read timeout=. requests'
timeout only bounds the gap between individual socket reads, so a server that
trickles data slowly enough to keep resetting that timer (without ever fully
stalling) can hang a call far longer than intended; this already produced one
confirmed multi-hour hang during an OAI-PMH harvest."""

import time

from django.test import SimpleTestCase

from works.harvesting.sessions import fetch_with_deadline


class FetchWithDeadlineTest(SimpleTestCase):
    def test_returns_result_when_fetch_completes_in_time(self):
        result = fetch_with_deadline(lambda: "ok", deadline=1)
        self.assertEqual(result, "ok")

    def test_raises_timeout_error_when_fetch_exceeds_deadline(self):
        def slow():
            time.sleep(2)
            return "too late"

        with self.assertRaises(TimeoutError):
            fetch_with_deadline(slow, deadline=0.1)

    def test_reraises_the_original_exception_from_fetch_fn(self):
        def broken():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            fetch_with_deadline(broken, deadline=1)
