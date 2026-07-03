# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import logging

from django.contrib.sessions.exceptions import SessionInterrupted
from django.http import HttpResponseRedirect

logger = logging.getLogger(__name__)


class SessionRaceRecoveryMiddleware:
    """Recover from a concurrent-request session race instead of crashing.

    Django's session middleware raises ``SessionInterrupted`` (a
    ``SuspiciousOperation``, normally surfaced as an HTTP 400) when two
    requests race on the same session: one cycles/deletes the session key
    (e.g. ``login()`` does this) while another, already in flight, still
    tries to save the now-deleted row. This happens in practice when a
    single link is fetched more than once nearly simultaneously (double
    click, duplicate tab, link-prefetching), and previously surfaced as a
    hard 400 error page. Redirecting back to the same path lets the losing
    request re-evaluate current session state instead of crashing.

    Must be listed before ``SessionMiddleware`` in ``MIDDLEWARE`` so its
    ``get_response`` call wraps ``SessionMiddleware``'s own response phase.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except SessionInterrupted:
            logger.warning("Recovered from a concurrent session race for %s", request.path)
            return HttpResponseRedirect(request.path)
