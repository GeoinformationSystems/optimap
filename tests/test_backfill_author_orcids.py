# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the ``backfill_author_orcids`` command and its Django-Q task.

Covers the sync path (links from OpenAlex), --dry-run, the --async enqueue, the
bounded-chunk re-enqueue, and the systemic-failure circuit breaker (abort +
staff email), mirroring the OpenAIRE backfill tests in test_openaire.py.
"""

from io import StringIO
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings

from works.models import Work
from works.tasks import backfill_author_orcids_task


def _openalex_session_returning(orcid="https://orcid.org/0000-0002-1825-0097"):
    """Fake OpenAlex session whose GET returns one author bearing ``orcid``."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "authorships": [{"author": {"display_name": "Jane Doe", "orcid": orcid, "id": "https://openalex.org/A123"}}]
    }
    session = MagicMock()
    session.get.return_value = resp
    return session


def _openalex_session_raising(status_code):
    """Fake OpenAlex session whose GET raises an HTTPError with ``status_code``."""
    resp = MagicMock()
    err = requests.HTTPError(f"{status_code} error")
    err.response = MagicMock(status_code=status_code)
    resp.raise_for_status.side_effect = err
    session = MagicMock()
    session.get.return_value = resp
    return session


class BackfillCommandTest(TestCase):
    def setUp(self):
        self.work = Work.objects.create(title="t", openalex_id="W1", status="h")

    @patch("works.tasks._openalex_session")
    def test_command_links_orcid_from_openalex(self, mock_session):
        mock_session.return_value = _openalex_session_returning()
        out = StringIO()
        call_command("backfill_author_orcids", "--source", "openalex", "--throttle", "0", stdout=out)
        self.assertEqual(self.work.author_links.count(), 1)
        self.assertTrue(self.work.author_links.filter(orcid="0000-0002-1825-0097").exists())
        self.assertIn("Linked", out.getvalue())

    @patch("works.tasks._openalex_session")
    def test_dry_run_writes_nothing(self, mock_session):
        mock_session.return_value = _openalex_session_returning()
        call_command(
            "backfill_author_orcids", "--source", "openalex", "--throttle", "0", "--dry-run", stdout=StringIO()
        )
        self.assertEqual(self.work.author_links.count(), 0)

    @patch(
        "works.management.commands.backfill_author_orcids.async_task",
        return_value="9f8c2a1b4d5e6f708192a3b4c5d6e7f8",
    )
    @patch("works.tasks._openalex_session")
    def test_async_enqueues_chunked_task_without_running(self, mock_session, mock_async):
        out = StringIO()
        call_command("backfill_author_orcids", "--source", "openalex", "--async", stdout=out)
        # --async must not touch OpenAlex in-process, and must not link anything.
        mock_session.assert_not_called()
        self.assertEqual(self.work.author_links.count(), 0)
        mock_async.assert_called_once()
        self.assertEqual(mock_async.call_args[0][0], "works.tasks.backfill_author_orcids_task")
        self.assertEqual(mock_async.call_args[1]["source"], "openalex")
        self.assertTrue(mock_async.call_args[1]["chunked"])
        # Output carries both the raw id and the humanized admin "Name".
        from django_q.humanhash import humanize

        output = out.getvalue()
        self.assertIn("9f8c2a1b4d5e6f708192a3b4c5d6e7f8", output)
        self.assertIn(humanize("9f8c2a1b4d5e6f708192a3b4c5d6e7f8"), output)


@override_settings(OPTIMAP_ORCID_BACKFILL_CHUNK_SECONDS=0)
class BackfillChunkingTest(TestCase):
    """When the per-chunk budget is exhausted, the task re-enqueues a continuation."""

    def setUp(self):
        for i in range(3):
            Work.objects.create(title=f"w{i}", openalex_id=f"W{i}", status="h")

    @patch("django_q.tasks.async_task")
    @patch("works.tasks._openalex_session")
    def test_reenqueues_with_cursor_and_carry(self, mock_session, mock_async):
        mock_session.return_value = _openalex_session_returning()
        backfill_author_orcids_task(source="openalex", throttle=0, chunked=True)
        # Budget 0 → one work processed, then a continuation is enqueued.
        mock_async.assert_called_once()
        self.assertEqual(mock_async.call_args[0][0], "works.tasks.backfill_author_orcids_task")
        self.assertGreater(mock_async.call_args[1]["after_id"], 0)
        self.assertEqual(mock_async.call_args[1]["carry"]["processed"], 1)
        self.assertEqual(mock_async.call_args[1]["source"], "openalex")


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    OPTIMAP_ORCID_BACKFILL_ABORT_AFTER=2,
)
class BackfillCircuitBreakerTest(TestCase):
    """A run of systemic upstream failures aborts, emails staff, and raises."""

    def setUp(self):
        get_user_model().objects.create_user(
            username="curator", email="curator@example.org", is_staff=True, is_active=True
        )
        for i in range(5):
            Work.objects.create(title=f"w{i}", openalex_id=f"W{i}", status="h")

    @patch("works.tasks._openalex_session")
    def test_aborts_emails_and_raises(self, mock_session):
        from works.tasks import _UpstreamUnavailable

        mock_session.return_value = _openalex_session_raising(429)
        with self.assertRaises(_UpstreamUnavailable):
            backfill_author_orcids_task(source="openalex", throttle=0)
        alerts = [m for m in mail.outbox if "Author ORCID backfill aborted" in m.subject]
        self.assertTrue(alerts)
        self.assertIn("curator@example.org", [addr for m in alerts for addr in m.to])
        self.assertIn("429", alerts[0].body)

    @patch("works.tasks._openalex_session")
    def test_404_is_not_systemic(self, mock_session):
        # A per-work miss (404) must not trip the breaker or send an alert.
        mock_session.return_value = _openalex_session_raising(404)
        summary = backfill_author_orcids_task(source="openalex", throttle=0)
        self.assertFalse(summary["aborted"])
        self.assertEqual(summary["errors"], 5)
        self.assertFalse([m for m in mail.outbox if "aborted" in m.subject])
