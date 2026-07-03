# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for admin email notification on first-time confirmed user registration.

Covers ``works.notifications.notify_admins_new_user_registered`` plus its wiring
inside ``authenticate_via_magic_link`` (only fresh accounts persisted via the
``?confirmed=true`` magic-link branch trigger the email).
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from works.models import EmailLog

User = get_user_model()


def _run_async_synchronously(*args, **kwargs):
    """Replacement for ``django_q.tasks.async_task`` — invokes the dotted-path
    function in-process so tests don't need a running cluster."""
    func_path = args[0]
    module_name, _, attr = func_path.rpartition(".")
    from importlib import import_module

    module = import_module(module_name)
    fn = getattr(module, attr)
    return fn(*args[1:], **kwargs)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_HOST_USER="optimap@example.org",
    BASE_URL="http://testserver",
)
class NewUserAdminNotificationTests(TestCase):
    """The magic-link view emails admins on first confirmed login."""

    def setUp(self):
        self.client = Client()
        self.admin1 = User.objects.create_user(
            username="admin1",
            email="admin1@optimap.example",
            password="x",
            is_staff=True,
        )
        self.admin2 = User.objects.create_user(
            username="admin2",
            email="admin2@optimap.example",
            password="x",
            is_staff=True,
        )
        # Sanity: a non-staff user already in the system should not receive notification.
        self.other = User.objects.create_user(
            username="other",
            email="other@example.org",
            password="x",
        )
        mail.outbox = []

    def _prime_magic_link(self, email: str, token: str = "tok-new"):
        cache.set(token, {"email": email, "next": "/"}, timeout=600)
        return token

    def test_first_confirmed_login_notifies_all_admins(self):
        token = self._prime_magic_link("brand-new@example.org")

        with patch("django_q.tasks.async_task", side_effect=_run_async_synchronously):
            response = self.client.post(reverse("optimap:magic_link", args=[token]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(email="brand-new@example.org").exists())

        recipients = sorted(addr for m in mail.outbox for addr in m.to)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(recipients, ["admin1@optimap.example", "admin2@optimap.example"])
        subject = mail.outbox[0].subject
        self.assertIn("New user registered", subject)
        self.assertIn("brand-new@example.org", subject)
        # Body carries the admin user page link.
        body = mail.outbox[0].body
        self.assertIn("brand-new@example.org", body)
        self.assertIn("/admin/works/customuser/", body)

        # EmailLog rows recorded.
        logs = EmailLog.objects.filter(subject__contains="New user registered")
        self.assertEqual(logs.count(), 2)
        self.assertTrue(all(log.status == "success" for log in logs))

    def test_existing_user_login_does_not_notify(self):
        """Returning users skip the create_user branch and must not trigger the email."""
        existing = User.objects.create_user(
            username="returning@example.org",
            email="returning@example.org",
        )
        token = self._prime_magic_link(existing.email, token="tok-returning")

        with patch("django_q.tasks.async_task", side_effect=_run_async_synchronously):
            response = self.client.post(reverse("optimap:magic_link", args=[token]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(EmailLog.objects.filter(subject__contains="New user registered").exists())

    def test_unconfirmed_first_visit_does_not_notify(self):
        """First leg of the new-account flow (no ?confirmed=true) renders the
        confirmation page and must not create the user or notify admins."""
        token = self._prime_magic_link("pending@example.org", token="tok-pending")

        with patch("django_q.tasks.async_task", side_effect=_run_async_synchronously):
            response = self.client.get(reverse("optimap:magic_link", args=[token]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="pending@example.org").exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_no_admins_no_error(self):
        """If no staff users exist, the notification is a no-op (login succeeds)."""
        User.objects.filter(is_staff=True).delete()
        token = self._prime_magic_link("loneranger@example.org")

        with patch("django_q.tasks.async_task", side_effect=_run_async_synchronously):
            response = self.client.post(reverse("optimap:magic_link", args=[token]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(email="loneranger@example.org").exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_staff_with_empty_email_skipped(self):
        """A staff user with no email address must not appear as a recipient."""
        User.objects.create_user(
            username="silent-admin",
            email="",
            password="x",
            is_staff=True,
        )
        token = self._prime_magic_link("freshie@example.org")

        with patch("django_q.tasks.async_task", side_effect=_run_async_synchronously):
            self.client.post(reverse("optimap:magic_link", args=[token]))

        recipients = sorted(addr for m in mail.outbox for addr in m.to)
        self.assertNotIn("", recipients)
        self.assertEqual(len(mail.outbox), 2)  # admin1 + admin2 only

    def test_inactive_staff_skipped(self):
        """A deactivated (is_active=False) staff account is not a recipient."""
        User.objects.create_user(
            username="inactive-admin",
            email="inactive-admin@optimap.example",
            password="x",
            is_staff=True,
            is_active=False,
        )
        token = self._prime_magic_link("fresh-active@example.org")

        with patch("django_q.tasks.async_task", side_effect=_run_async_synchronously):
            self.client.post(reverse("optimap:magic_link", args=[token]))

        recipients = sorted(addr for m in mail.outbox for addr in m.to)
        self.assertNotIn("inactive-admin@optimap.example", recipients)
        self.assertEqual(len(mail.outbox), 2)  # admin1 + admin2 only

    def test_opted_out_staff_skipped(self):
        """A staff user with notify_work_events=False does not get the email."""
        self.admin2.userprofile.notify_work_events = False
        self.admin2.userprofile.save()
        token = self._prime_magic_link("fresh-optout@example.org")

        with patch("django_q.tasks.async_task", side_effect=_run_async_synchronously):
            self.client.post(reverse("optimap:magic_link", args=[token]))

        recipients = sorted(addr for m in mail.outbox for addr in m.to)
        self.assertNotIn("admin2@optimap.example", recipients)
        self.assertEqual(recipients, ["admin1@optimap.example"])

    def test_notification_failure_does_not_block_login(self):
        """If async_task raises, the user must still be created and logged in
        (notify_* is wrapped in defensive try/except)."""
        token = self._prime_magic_link("resilient@example.org")

        with patch(
            "django_q.tasks.async_task",
            side_effect=RuntimeError("queue offline"),
        ):
            response = self.client.post(reverse("optimap:magic_link", args=[token]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(email="resilient@example.org").exists())

    def test_existing_user_normalised_email_is_found(self):
        """loginres normalises email to lowercase before caching, so the exact
        lookup in authenticate_via_magic_link always sees a lowercase value and
        finds the existing account without showing the consent screen."""
        User.objects.create_user(
            username="case@example.org",
            email="case@example.org",
        )
        # Simulate loginres: the original input "Case@Example.ORG" is lowercased
        # before being written to cache.
        token = self._prime_magic_link("case@example.org", token="tok-case")

        with patch("django_q.tasks.async_task", side_effect=_run_async_synchronously):
            response = self.client.post(reverse("optimap:magic_link", args=[token]))

        self.assertEqual(response.status_code, 302)
        # No duplicate created.
        self.assertEqual(User.objects.filter(email="case@example.org").count(), 1)
        # No new-user notification for a returning user.
        self.assertEqual(len(mail.outbox), 0)

    def test_consented_at_set_on_first_confirmed_login(self):
        """consented_at is stamped on the UserProfile when a new user clicks 'I consent'."""
        token = self._prime_magic_link("newcomer@example.org")

        with patch("django_q.tasks.async_task", side_effect=_run_async_synchronously):
            response = self.client.post(reverse("optimap:magic_link", args=[token]))

        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email="newcomer@example.org")
        self.assertIsNotNone(user.userprofile.consented_at)

    def test_consented_at_not_overwritten_on_returning_login(self):
        """Returning users skip the elif branch; consented_at remains unchanged."""
        from works.models import UserProfile

        existing = User.objects.create_user(
            username="veteran@example.org",
            email="veteran@example.org",
        )
        token = self._prime_magic_link(existing.email, token="tok-veteran")

        with patch("django_q.tasks.async_task", side_effect=_run_async_synchronously):
            self.client.post(reverse("optimap:magic_link", args=[token]))

        profile = UserProfile.objects.get(user=existing)
        self.assertIsNone(profile.consented_at)

    def test_profile_created_for_user_without_one(self):
        """Signal get_or_create guard: updating a legacy user without a UserProfile
        must not raise RelatedObjectDoesNotExist."""
        from works.models import UserProfile

        existing = User.objects.create_user(
            username="legacy@example.org",
            email="legacy@example.org",
        )
        # Simulate a legacy account by deleting its auto-created profile.
        UserProfile.objects.filter(user=existing).delete()
        self.assertFalse(UserProfile.objects.filter(user=existing).exists())

        # Saving the user (as login_user does) must not raise.
        existing.save()

        self.assertTrue(UserProfile.objects.filter(user=existing).exists())

    def test_confirmed_true_for_existing_user_does_not_crash(self):
        """If ?confirmed=true reaches the view for an already-existing account
        (e.g. direct URL manipulation or a stale link), the view must log the
        user in rather than raising IntegrityError (the original bug).

        With normalised email the initial lookup succeeds, so the elif branch
        is skipped entirely — but the test guards against regression."""
        User.objects.create_user(
            username="dup@example.org",
            email="dup@example.org",
        )
        # Cache holds the normalised (lowercase) email, as loginres produces.
        token = self._prime_magic_link("dup@example.org", token="tok-dup")

        with patch("django_q.tasks.async_task", side_effect=_run_async_synchronously):
            response = self.client.post(reverse("optimap:magic_link", args=[token]))

        self.assertEqual(response.status_code, 302)
        # Exactly one user — no duplicate.
        self.assertEqual(User.objects.filter(email="dup@example.org").count(), 1)
        # No new-user email for an already-existing account.
        self.assertFalse(EmailLog.objects.filter(subject__contains="New user registered").exists())
