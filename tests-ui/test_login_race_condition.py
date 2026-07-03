# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression test for a magic-link login crash observed in production.

A single magic link was fetched more than once in quick succession (e.g. a
duplicate browser request, or a mail client/security gateway prefetching the
link), sharing the same (pre-existing) session cookie. Because the old view
called ``login()`` -- which cycles the session key -- on every plain ``GET``,
two concurrent requests could race: one deletes/replaces the session row
while the other is still trying to save it, which Django surfaces as
``SessionInterrupted`` -> HTTP 400 (see the "Forced update did not affect any
rows" / "SessionInterrupted" traceback this test reproduces).

The fix makes ``GET /login/<token>`` side-effect free (it only renders a
confirmation page); real authentication only happens on ``POST``. This test
drives the live server directly over HTTP (not Selenium) because the race
requires genuine concurrent requests hitting the same session row.
"""

import threading

import requests
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache

User = get_user_model()


class MagicLinkConcurrentGetTest(StaticLiveServerTestCase):
    def setUp(self):
        self.email = "race@example.com"
        self.user = User.objects.create_user(username=self.email, email=self.email)
        self.token = "race-condition-token"
        cache.set(self.token, {"email": self.email, "next": "/"}, timeout=300)

        # Simulate a browser that already has an anonymous session cookie
        # (e.g. from visiting the homepage before clicking the emailed
        # link) -- this is the shared session two concurrent requests race
        # on in the original bug.
        session = SessionStore()
        session.create()
        self.session_key = session.session_key

    def tearDown(self):
        User.objects.filter(email=self.email).delete()
        cache.delete(self.token)

    def test_concurrent_get_requests_do_not_crash_with_400(self):
        url = f"{self.live_server_url}/login/{self.token}"
        results = []
        lock = threading.Lock()

        def fetch():
            session = requests.Session()
            session.cookies.set("sessionid", self.session_key)
            try:
                resp = session.get(url, allow_redirects=False, timeout=15)
                status = resp.status_code
            except requests.RequestException as exc:
                status = exc
            with lock:
                results.append(status)

        threads = [threading.Thread(target=fetch) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self.assertEqual(len(results), 8, f"not all requests completed: {results}")
        for status in results:
            self.assertNotEqual(
                status,
                400,
                f"a concurrent GET to the magic link crashed with 400 (session race); all statuses: {results}",
            )
            # GET must not authenticate: expect the confirmation page (200),
            # never a redirect (a GET-triggered login would 302).
            self.assertEqual(status, 200, f"unexpected status for a GET login link: {results}")

        # Because GET is now side-effect free, the user must still be
        # anonymous -- no request logged them in.
        self.assertIsNone(User.objects.get(email=self.email).last_login)
