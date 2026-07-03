# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Authentication and user account management views.

This module handles:
- Magic link authentication
- User login/logout
- User settings and subscriptions
- Email changes
- Account deletion
"""

import logging

logger = logging.getLogger(__name__)

import imaplib
import secrets
import time
import uuid
from math import floor
from urllib.parse import unquote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage, get_connection, send_mail
from django.core.validators import EmailValidator
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from works.models import (
    BlockedDomain,
    BlockedEmail,
    Collection,
    Contribution,
    GlobalRegion,
    Source,
    Subscription,
    UserProfile,
)
from works.recognition import (
    RECOGNITION_TIERS,
    USERNAME_REGEX,
    generate_random_username,
    group_by_tier,
    is_offensive,
)
from works.utils.email import render_email

User = get_user_model()

# Constants
LOGIN_TOKEN_LENGTH = 32
LOGIN_TOKEN_TIMEOUT_SECONDS = 10 * 60
EMAIL_CONFIRMATION_TIMEOUT_SECONDS = 10 * 60
ACCOUNT_DELETE_TOKEN_TIMEOUT_SECONDS = 10 * 60
USER_DELETE_TOKEN_PREFIX = "user_delete_token"
EMAIL_CONFIRMATION_TOKEN_PREFIX = "email_confirmation"

email_validator = EmailValidator()


def loginres(request):
    if request.method != "POST":
        return redirect("/")
    email = request.POST.get("email", "").lower().strip()
    try:
        email_validator(email)
    except ValidationError:
        return render(
            request,
            "error.html",
            {
                "error": {
                    "class": "danger",
                    "title": "Login failed!",
                    "text": "The email address you entered is not valid. Please enter a valid email address.",
                }
            },
        )
    if is_email_blocked(email):
        logger.warning("Attempted login with blocked email: %s", email)
        return render(
            request,
            "error.html",
            {
                "error": {
                    "class": "danger",
                    "title": "Login failed!",
                    "text": f'You attempted to login using an email that is blocked. Please contact support for assistance: <a href="{request.site.domain}/contact">{request.site.domain}/contact</a>',
                }
            },
        )
    else:
        link = get_login_link(request, email)
        valid = floor(LOGIN_TOKEN_TIMEOUT_SECONDS / 60)
        subject, body = render_email(
            "email/magic_link.en.txt",
            {
                "email": email,
                "domain": request.site.domain,
                "link": link,
                "valid": valid,
            },
        )
        logger.info("Login process started for user %s", email)
        # Login (and the other auth-adjacent flows below) sends the email
        # synchronously on purpose: SMTP failure must surface to the user
        # immediately as the "Login failed!" error page so they can retry,
        # and the magic-link cache TTL (10 min) is short enough that a
        # Django-Q dispatch lag would eat into the window. Other email
        # call sites in OPTIMAP (harvest, monthly digest, work-state
        # notifications) are background concerns and do go through Django-Q.
        try:
            email_message = EmailMessage(
                subject=subject,
                body=body,
                from_email=settings.EMAIL_HOST_USER,
                to=[email],
                headers={"OPTIMAP": request.site.domain},
            )
            result = email_message.send()
            logger.info(
                "%s sent login email to %s with the result: %s",
                settings.EMAIL_HOST_USER,
                email_message.recipients(),
                result,
            )
        except Exception as ex:
            logger.exception("Error sending login email to %s from %s", email, settings.EMAIL_HOST_USER)
            logger.error(ex)
            return render(
                request,
                "error.html",
                {
                    "error": {
                        "class": "danger",
                        "title": "Login failed!",
                        "text": "Error sending the login email. Please try again or contact us!",
                    }
                },
            )

        try:
            if str(get_connection().__class__.__module__).endswith("smtp"):
                with imaplib.IMAP4_SSL(settings.EMAIL_HOST_IMAP, port=settings.EMAIL_PORT_IMAP) as imap:
                    message = str(email_message.message()).encode()
                    imap.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
                    folder = settings.EMAIL_IMAP_SENT_FOLDER
                    imap.append(
                        '"{folder}"', "\\Seen", imaplib.Time2Internaldate(time.time()), str(message).encode("utf-8")
                    )
                    logger.debug('Saved email to IMAP folder "%s"', folder)
        except Exception as ex:
            logger.exception("Error saving sent email to %s for %s", email, settings.EMAIL_HOST_USER)
            logger.error(ex)

        messages.success(
            request,
            f"We sent a login link to {email}. Please check your email and click the link within {valid} minutes.",
            extra_tags="persist",
        )
        return redirect("/")


def confirmation_login(request):
    return render(request, "confirmation_login.html")


def login_user(request, user):
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    user.save()


@require_http_methods(["GET", "POST"])
def authenticate_via_magic_link(request, token):
    cache_data = cache.get(token)
    logger.info("Authenticating magic link with token %s: Found data: %s", token, cache_data)

    if cache_data is None:
        logger.debug("Magic link invalid or expired")
        return render(
            request,
            "error.html",
            {
                "error": {
                    "class": "danger",
                    "title": "Authentication failed!",
                    "text": "Magic link invalid or expired. Please try again!",
                }
            },
        )

    # Extract email and next URL from cache data
    email = cache_data.get("email")
    next_url = cache_data.get("next", "/")

    # Ensure next_url is valid - default to home if empty or invalid
    if not next_url or next_url.strip() == "":
        next_url = "/"

    user = User.objects.filter(email=email).first()
    is_new = user is None

    # GET never authenticates: it only shows a confirmation page. This is
    # deliberate — a GET can be triggered by something other than the
    # intended human click (double-click, duplicate tab, a mail client/
    # security gateway prefetching links in the email before delivery), and
    # letting that silently log a user in previously caused two problems:
    # a security issue (an automated fetch could consume the one-time link),
    # and a crash (concurrent GETs both calling login() raced on Django's
    # session cycling and raised SessionInterrupted). Actual authentication
    # only happens on the POST below, which requires a CSRF token and is not
    # something a plain link-follower can trigger.
    if request.method == "GET":
        return render(
            request,
            "confirmation_login.html",
            {"email": email, "token": token, "is_new": is_new, "next": next_url},
        )

    if is_new:
        user, created = User.objects.get_or_create(
            username=email,
            defaults={"username": email, "email": email},
        )
        if not created and not user.email:
            user.email = email
            user.save()
        # First-time confirmed registration: tell the admins. Imported
        # locally to keep this view import-graph free of django_q at import
        # time. Errors inside notify_* never propagate (defensive try/except).
        if created:
            from works.notifications import notify_admins_new_user_registered

            notify_admins_new_user_registered(user)
            UserProfile.objects.filter(user=user).update(consented_at=timezone.now())
        login_user(request, user)
        cache.delete(token)
        messages.success(request, "Welcome to OPTIMAP!")
        logger.info("User %s logged in successfully, redirecting to %s", email, next_url)
    else:
        login_user(request, user)
        cache.delete(token)
        messages.success(request, "Welcome back to OPTIMAP!")
        logger.info("User %s authenticated, redirecting to %s", email, next_url)

    return HttpResponseRedirect(next_url)


@login_required
def customlogout(request):
    logout(request)
    messages.info(request, "You have successfully logged out.")
    return redirect("/")


@never_cache
def user_settings(request):
    if not request.user.is_authenticated:
        return render(
            request,
            "error.html",
            {
                "error": {
                    "class": "warning",
                    "title": "Login required.",
                    "text": "Please log in using the menu in the top-right to access your settings.",
                },
            },
        )

    profile, created = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        form = request.POST.get("form", "notifications")
        if form == "recognition":
            opt_in = request.POST.get("recognition_opt_in") == "on"
            raw_username = (request.POST.get("recognition_username") or "").strip()

            if opt_in:
                # Auto-generate a default if the user enabled the toggle without supplying one.
                if not raw_username:
                    raw_username = generate_random_username()
                if not USERNAME_REGEX.match(raw_username):
                    messages.error(
                        request,
                        "Recognition Board username must be 3–64 characters of letters, digits, '-' or '_'.",
                    )
                    return redirect(reverse("optimap:usersettings"))
                if is_offensive(raw_username):
                    messages.error(request, "Please choose a different username.")
                    return redirect(reverse("optimap:usersettings"))
                if UserProfile.objects.filter(recognition_username=raw_username).exclude(pk=profile.pk).exists():
                    messages.error(request, "That Recognition Board username is already taken.")
                    return redirect(reverse("optimap:usersettings"))
                profile.recognition_opt_in = True
                profile.recognition_username = raw_username
            else:
                profile.recognition_opt_in = False
                # Keep the stored username so opting back in doesn't lose it.
            profile.save()
            messages.success(request, "Recognition Board settings saved.")
        else:
            profile.notify_new_manuscripts = request.POST.get("notify_new_manuscripts") == "on"
            profile.notify_work_events = request.POST.get("notify_work_events") == "on"
            profile.save()
            messages.success(request, "Notification settings saved.")
        return redirect(reverse("optimap:usersettings"))
    return render(
        request,
        "user_settings.html",
        {
            "profile": profile,
            "delete_token": request.session.get(USER_DELETE_TOKEN_PREFIX, None),
        },
    )


@never_cache
@login_required
@require_GET
def random_recognition_username(request):
    """Return a freshly generated random username as JSON.

    Used by the settings page to populate the username field when a user opts in
    or clicks "Generate". The returned name is a candidate — uniqueness is
    re-validated when the form is submitted.
    """
    return JsonResponse({"username": generate_random_username()})


def recognition_board(request):
    """Public contributor recognition board, grouped into 5 explorer-named tiers."""

    profiles = (
        UserProfile.objects.filter(recognition_opt_in=True, recognition_username__isnull=False)
        .annotate(
            total=Count("user__contributions"),
            spatial_count=Count(
                "user__contributions",
                filter=Q(user__contributions__kind=Contribution.SPATIAL),
            ),
            temporal_count=Count(
                "user__contributions",
                filter=Q(user__contributions__kind=Contribution.TEMPORAL),
            ),
            doi_count=Count(
                "user__contributions",
                filter=Q(user__contributions__kind=Contribution.DOI),
            ),
        )
        .filter(total__gt=0)
        .order_by("-total", "recognition_username")
    )
    grouped = group_by_tier(profiles)
    return render(
        request,
        "recognition_board.html",
        {
            "tiers": grouped,
            "all_tiers": RECOGNITION_TIERS,
        },
    )


@never_cache
@login_required
def user_subscriptions(request):
    """Display and manage user's regional subscriptions.

    ``@never_cache`` keeps the site-wide UpdateCacheMiddleware from caching
    the rendered checkbox state per session cookie — without it, after the
    user POSTs to ``/addsubscriptions/`` and gets redirected back here, the
    middleware short-circuits with the pre-update HTML and the checkboxes
    appear unchanged until ``CACHE_MIDDLEWARE_SECONDS`` expires.
    """
    user = request.user

    # Get or create the user's subscription
    subscription, created = Subscription.objects.get_or_create(
        user=user, defaults={"name": f"{user.username}_subscription"}
    )

    # Get all available regions, grouped by type
    continents = GlobalRegion.objects.filter(region_type=GlobalRegion.CONTINENT).order_by("name")
    oceans = GlobalRegion.objects.filter(region_type=GlobalRegion.OCEAN).order_by("name")

    # Sources and published collections, alphabetical, annotated with their
    # published-work count for display next to each checkbox.
    published_work_count = Count("works", filter=Q(works__status="p"))
    sources = Source.objects.annotate(published_work_count=published_work_count).order_by("name")
    collections = (
        Collection.objects.filter(is_published=True)
        .annotate(published_work_count=published_work_count)
        .order_by("name")
    )

    # Get user's currently selected regions/sources/collections
    selected_region_ids = list(subscription.regions.values_list("id", flat=True))
    selected_source_ids = list(subscription.sources.values_list("id", flat=True))
    selected_collection_ids = list(subscription.collections.values_list("id", flat=True))

    context = {
        "subscription": subscription,
        "continents": continents,
        "oceans": oceans,
        "sources": sources,
        "collections": collections,
        "selected_region_ids": selected_region_ids,
        "selected_source_ids": selected_source_ids,
        "selected_collection_ids": selected_collection_ids,
        "notification_interval": subscription.notification_interval,
    }

    return render(request, "subscriptions.html", context)


@login_required
def add_subscriptions(request):
    """Update user's regional subscriptions."""
    if request.method == "POST":
        user = request.user

        # Get or create the user's subscription
        subscription, created = Subscription.objects.get_or_create(
            user=user, defaults={"name": f"{user.username}_subscription"}
        )

        # Get selected region/source/collection IDs from the form
        selected_region_ids = request.POST.getlist("regions")
        selected_source_ids = request.POST.getlist("sources")
        selected_collection_ids = request.POST.getlist("collections")

        # Update the subscription's regions
        subscription.regions.clear()
        if selected_region_ids:
            regions = GlobalRegion.objects.filter(id__in=selected_region_ids)
            subscription.regions.set(regions)

        subscription.sources.clear()
        if selected_source_ids:
            sources = Source.objects.filter(id__in=selected_source_ids)
            subscription.sources.set(sources)

        subscription.collections.clear()
        if selected_collection_ids:
            collections = Collection.objects.filter(id__in=selected_collection_ids, is_published=True)
            subscription.collections.set(collections)

        raw_interval = request.POST.get("notification_interval", "monthly")
        subscription.notification_interval = raw_interval if raw_interval in ("weekly", "monthly") else "monthly"
        subscription.save(update_fields=["notification_interval"])

        # Report what was actually persisted, not the raw POSTed id lists — ids that
        # don't exist (or, for collections, aren't published) are silently dropped
        # by the .filter() calls above and shouldn't be claimed as "monitored".
        region_count = subscription.regions.count()
        source_count = subscription.sources.count()
        collection_count = subscription.collections.count()

        logger.info(
            "Updated subscription for user %s with %d regions, %d sources, %d collections",
            user.username,
            region_count,
            source_count,
            collection_count,
        )
        messages.success(
            request,
            f"Subscription updated! Monitoring {region_count} regions, "
            f"{source_count} sources, {collection_count} collections.",
        )

        return HttpResponseRedirect("/subscriptions/")

    return HttpResponseRedirect("/subscriptions/")


@login_required
def unsubscribe(request):
    """Handles unsubscription requests from emails."""
    user = request.user
    search_term = request.GET.get("search")
    unsubscribe_all = request.GET.get("all")

    if unsubscribe_all:
        Subscription.objects.filter(user=user).update(subscribed=False)
        messages.success(request, "You have been unsubscribed from all subscriptions.")
        return redirect("/")
    if search_term:
        exact_search_term = unquote(search_term).strip()
        subscription = get_object_or_404(Subscription, user=user, search_term=exact_search_term)
        if not subscription:
            messages.warning(request, f"No subscription found for '{search_term}'.")
            return redirect("/")
        subscription.subscribed = False
        subscription.save()
        messages.success(request, f"You have unsubscribed from '{search_term}'.")
        return redirect("/")

    return HttpResponse("Invalid request.", status=400)


@login_required
def change_useremail(request):
    email_new = request.POST.get("email_new", "").lower().strip()
    currentuser = request.user
    email_old = currentuser.email
    try:
        email_validator(email_new)
    except ValidationError:
        return render(
            request,
            "error.html",
            {
                "error": {
                    "class": "danger",
                    "title": "Invalid Email Change!",
                    "text": "The email address you entered is not valid. Please enter a valid email address.",
                }
            },
        )
    if is_email_blocked(email_new):
        logger.warning("Attempted login with blocked email: %s", email_new)
        return render(
            request,
            "error.html",
            {
                "error": {
                    "class": "danger",
                    "title": "Login failed!",
                    "text": "You attempted to change your email to an address that is blocked. Please contact support for assistance.",
                }
            },
        )
    if not email_new or email_new == email_old:
        messages.error(request, "Invalid email change request.")
        return render(
            request,
            "error.html",
            {
                "error": {
                    "class": "danger",
                    "title": "Invalid Email Change!",
                    "text": "You attempted to change your email to an address that is invalid. Please enter a valid email address that is different from the current one.",
                }
            },
        )
    if User.objects.filter(email=email_new).exists():
        messages.error(request, "This email is already in use.")
        return render(
            request,
            "error.html",
            {
                "error": {
                    "class": "danger",
                    "title": "Email Already In Use!",
                    "text": "You attempted to change your email to an address that is already in use.",
                }
            },
        )

    token = secrets.token_urlsafe(32)
    cache.set(
        f"{EMAIL_CONFIRMATION_TOKEN_PREFIX}_{email_new}",
        {"token": token, "old_email": request.user.email},
        timeout=EMAIL_CONFIRMATION_TIMEOUT_SECONDS,
    )
    confirm_url = request.build_absolute_uri(reverse("optimap:confirm_email_change", args=[token, email_new]))
    subject, message = render_email(
        "email/email_change_confirm.en.txt",
        {
            "email_old": email_old,
            "email_new": email_new,
            "confirm_url": confirm_url,
        },
    )
    send_mail(subject, message, settings.EMAIL_HOST_USER, [email_new])
    messages.info(
        request,
        f"We sent a confirmation link to {email_new}. Please click it to complete the email change.",
        extra_tags="persist",
    )
    logout(request)
    return redirect("/")


def confirm_email_change(request, token, email_new):
    cached_data = cache.get(f"{EMAIL_CONFIRMATION_TOKEN_PREFIX}_{email_new}")
    if not cached_data:
        messages.error(request, "Invalid or expired confirmation link.")
        return HttpResponseRedirect("/")
    if isinstance(cached_data, str):
        messages.error(request, "Cache error: Expected dictionary, got string.")
        return HttpResponseRedirect("/")
    stored_token = cached_data.get("token")
    old_email = cached_data.get("old_email")
    if stored_token != token:
        messages.error(request, "Invalid or expired confirmation link.")
        return HttpResponseRedirect("/")
    user = User.objects.filter(email=old_email).first()
    if not user:
        messages.error(request, "User not found.")
        return HttpResponseRedirect("/")
    user.email = email_new
    user.username = email_new
    user.save()
    notify_subject, notify_message = render_email(
        "email/email_changed_notify.en.txt",
        {
            "old_email": old_email,
            "email_new": email_new,
            "contact_url": f"{settings.BASE_URL}/contact",
        },
    )
    send_mail(notify_subject, notify_message, from_email=settings.EMAIL_HOST_USER, recipient_list=[old_email])
    cache.delete(f"{EMAIL_CONFIRMATION_TOKEN_PREFIX}_{email_new}")
    login_user(request, user)
    messages.success(request, "Your email has been successfully updated!")
    return redirect("/usersettings/")


def get_login_link(request, email):
    token = secrets.token_urlsafe(nbytes=LOGIN_TOKEN_LENGTH)
    link = f"{request.scheme}://{request.site.domain}/login/{token}"

    # Store both email and next parameter in cache
    next_url = request.GET.get("next", request.POST.get("next", ""))
    # Ensure next_url is never empty - default to home page
    if not next_url or next_url.strip() == "":
        next_url = "/"
    cache_data = {"email": email, "next": next_url}
    cache.set(token, cache_data, timeout=LOGIN_TOKEN_TIMEOUT_SECONDS)

    logger.info("Created login link for %s with token %s (next=%s) - %s", email, token, next_url, link)
    return link


def is_email_blocked(email):
    domain = email.split("@")[-1]
    if BlockedEmail.objects.filter(email__iexact=email).exists():
        return True
    if BlockedDomain.objects.filter(domain=domain).exists():
        return True
    return False


@login_required
def request_delete(request):
    user = request.user
    token = uuid.uuid4().hex
    cache.set(f"{USER_DELETE_TOKEN_PREFIX}_{token}", user.id, timeout=ACCOUNT_DELETE_TOKEN_TIMEOUT_SECONDS)
    confirm_url = request.build_absolute_uri(reverse("optimap:confirm_delete", args=[token]))
    timeout_minutes = ACCOUNT_DELETE_TOKEN_TIMEOUT_SECONDS // 60
    subject, deletion_body = render_email(
        "email/account_deletion_confirm.en.txt",
        {
            "confirm_url": confirm_url,
            "timeout_minutes": timeout_minutes,
        },
    )
    send_mail(subject, deletion_body, "no-reply@optimap.com", [user.email])
    return redirect(reverse("optimap:usersettings") + "?message=Check your email for a confirmation link.")


@login_required(login_url="/")
def confirm_account_deletion(request, token):
    try:
        user_id = cache.get(f"{USER_DELETE_TOKEN_PREFIX}_{token}")
        if user_id is None:
            messages.error(request, "Invalid or expired deletion token.")
            return redirect(reverse("optimap:usersettings"))
        if request.user.id != user_id:
            messages.error(request, "You are not authorized to delete this account.")
            return redirect(reverse("optimap:main"))
        request.session[USER_DELETE_TOKEN_PREFIX] = token
        request.session.modified = True
        request.session.save()
        messages.warning(
            request, "Please confirm your account deletion. Your contributed data will remain on the platform."
        )
        return redirect(reverse("optimap:usersettings"))
    except Exception as e:
        messages.error(request, f"An error occurred: {str(e)}")
        return redirect(reverse("optimap:usersettings"))


@login_required(login_url="/")
def finalize_account_deletion(request):
    token = request.session.get(USER_DELETE_TOKEN_PREFIX)
    if not token:
        messages.error(request, "No active deletion request found.")
        return redirect(reverse("optimap:usersettings"))
    user_id = cache.get(f"{USER_DELETE_TOKEN_PREFIX}_{token}")
    if user_id is None:
        messages.error(request, "Invalid or expired deletion request.")
        return redirect(reverse("optimap:usersettings"))
    if request.user.id != user_id:
        messages.error(request, "You are not authorized to delete this account.")
        return redirect(reverse("optimap:main"))
    user = get_object_or_404(User, id=user_id)
    try:
        user.delete()
        logout(request)
        messages.success(request, "Your account has been successfully deleted.")
        return redirect(reverse("optimap:main"))
    except Exception as e:
        logger.error(f"Error deleting user {user.email}: {str(e)}")
        messages.error(request, "An error occurred while deleting your account. Please try again.")
        return redirect(reverse("optimap:usersettings"))
    finally:
        cache.delete(f"{USER_DELETE_TOKEN_PREFIX}_{token}")
        if USER_DELETE_TOKEN_PREFIX in request.session:
            del request.session[USER_DELETE_TOKEN_PREFIX]
            request.session.modified = True
