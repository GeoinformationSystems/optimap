# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for works.signals.notify_on_task_failure — the generic Django-Q
post_execute failure notifier (issue #130)."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings

from works.signals import notify_on_task_failure

User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", ADMINS=[])
class NotifyOnTaskFailureTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="onfailure@example.com", email="onfailure@example.com", password="p", is_staff=True
        )

    def test_success_task_does_not_notify(self):
        task = {"id": "x" * 32, "func": "works.tasks.regenerate_all_data_dumps", "success": True}
        notify_on_task_failure(sender="django_q", task=task)
        self.assertEqual(len(mail.outbox), 0)

    def test_failed_task_notifies_active_staff(self):
        task = {
            "id": "y" * 32,
            "func": "works.tasks.some_random_task",
            "success": False,
            "group": "misc",
            "stopped": None,
            "result": "RuntimeError: boom",
        }
        notify_on_task_failure(sender="django_q", task=task)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.staff.email])
        self.assertIn("works.tasks.some_random_task", mail.outbox[0].body)
        self.assertIn("boom", mail.outbox[0].body)
        # Regression: an admin reading this out of context (forwarded, or just
        # the subject line) has no way to tell which deployment sent it.
        self.assertIn(settings.BASE_URL, mail.outbox[0].body)

    def test_self_notifying_func_is_skipped(self):
        task = {
            "id": "z" * 32,
            "func": "works.tasks.enrich_openaire_backfill",
            "success": False,
            "result": "OpenAireUnavailable: aborted",
        }
        notify_on_task_failure(sender="django_q", task=task)
        self.assertEqual(len(mail.outbox), 0)

    def test_no_staff_configured_does_not_crash(self):
        User.objects.filter(is_staff=True).update(is_staff=False)
        task = {"id": "w" * 32, "func": "works.tasks.some_task", "success": False, "result": "boom"}
        notify_on_task_failure(sender="django_q", task=task)  # must not raise
        self.assertEqual(len(mail.outbox), 0)
