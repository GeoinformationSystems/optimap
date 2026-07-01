# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Django-Q task entry points.

The harvesting code lives in the ``works.harvesting`` package — one module
per source type (OAI-PMH / RSS / Crossref / MaRESS) plus shared helpers.
This module re-exports the public surface so existing dotted-path schedules
(``works.tasks.harvest_oai_endpoint`` etc.), test imports, and ``@patch``
targets keep resolving without migration.

The non-harvest tasks (monthly email digest, subscription emails, GeoJSON /
GeoPackage cache regeneration, schedule helpers) still live here.
"""

import calendar
import glob
import gzip
import json
import logging
import os
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage, send_mail
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django_q.models import Schedule
from django_q.tasks import schedule
from osgeo import gdal

# -----------------------------------------------------------------------------
# Re-exports from the harvesting package — preserve the public surface that
# Django-Q schedule rows (`works.tasks.harvest_*`), tests, and admin actions
# reference by dotted path.
# -----------------------------------------------------------------------------
from works.harvesting.common import (  # noqa: F401
    HarvestStats,
    HarvestWarningCollector,
    _carefully_update_work,
    _find_existing_work,
    _get_article_link,
    _is_empty_for_update,
    _save_or_update_work,
    complete_harvest,
    fail_harvest,
    get_or_create_admin_command_user,
    parse_publication_date,
    resolve_user,
    send_harvest_email,
)
from works.harvesting.crossref import (  # noqa: F401
    CROSSREF_API_URL,
    CROSSREF_HTTP_TIMEOUT,
    CROSSREF_PAGE_ROWS,
    _build_crossref_filter,
    _crossref_item_to_work_kwargs,
    _crossref_session,
    _strip_jats,
    fetch_copernicus_abstract,
    harvest_crossref_book_list,
    harvest_crossref_doi,
    harvest_crossref_prefix,
    parse_crossref_response_and_save_works,
    reharvest_work,
)
from works.harvesting.geoscienceworld import (  # noqa: F401
    harvest_geoscienceworld,
    parse_gsw_response_and_save_works,
)
from works.harvesting.metadata_html import (  # noqa: F401
    _extract_dc_box,
    _extract_dc_spatial_coverage,
    _extract_dc_temporal,
    _extract_geojson_link,
    _extract_jsonld_spatial,
    _extract_jsonld_temporal,
    _geom_from_geojson_dict,
    _polygon_from_bbox,
    _split_iso_interval,
    _walk_jsonld,
    _wrap_in_collection,
    extract_geometry_from_html,
    extract_timeperiod_from_html,
)
from works.harvesting.mountain_wetlands import (  # noqa: F401
    MWR_HTTP_TIMEOUT,
    MWR_PAGE_SIZE,
    _mwr_authors_list,
    _mwr_first_author_surname,
    _mwr_geometry_from_study_sites,
    _mwr_item_url,
    _mwr_publication_year,
    _mwr_session,
    harvest_mountain_wetlands,
    parse_mountain_wetlands_response_and_save_works,
)
from works.harvesting.oai import (  # noqa: F401
    DOI_REGEX,
    harvest_oai_endpoint,
    parse_oai_xml_and_save_works,
)
from works.harvesting.openaire import (  # noqa: F401
    build_openaire_fields,
    enrich_event_from_openaire,
    enrich_work_from_openaire,
    fetch_openaire_record,
)
from works.harvesting.openalex import build_openalex_fields  # noqa: F401
from works.harvesting.openalex_source import (  # noqa: F401
    OPENALEX_API_URL,
    OPENALEX_HTTP_TIMEOUT,
    OPENALEX_PAGE_SIZE,
    _openalex_session,
    harvest_openalex_source,
    parse_openalex_response_and_save_works,
)
from works.harvesting.rss import (  # noqa: F401
    harvest_rss_endpoint,
    parse_rss_feed_and_save_publications,
)
from works.harvesting.sessions import (  # noqa: F401
    OAI_HTTP_TIMEOUT,
    OAI_RETRY_TOTAL,
    OAI_USER_AGENT,
    _looks_like_oai_xml,
    _oai_session,
    _openaire_session,
    _short_body,
)
from works.models import EmailLog, Subscription, Work
from works.utils.email import render_email
from works.utils.geojson import _GEOJSON_METADATA
from works.utils.geometry import annotate_rounded_geometry, round_geojson_coordinates
from works.utils.scheduling import log_scheduled_catchup

logger = logging.getLogger(__name__)
BASE_URL = settings.BASE_URL

# Wrap the re-exported harvest entry points so recurring schedules
# (`works.tasks.harvest_*`, which set intended_date_kwarg="scheduled_for") log a
# catch-up notice when they fire late after downtime. Re-binding the names here
# is what Django-Q resolves via dotted path; manual/ad-hoc calls (no
# scheduled_for) are unaffected. See works/utils/scheduling.py.
harvest_oai_endpoint = log_scheduled_catchup(harvest_oai_endpoint)
harvest_rss_endpoint = log_scheduled_catchup(harvest_rss_endpoint)
harvest_crossref_prefix = log_scheduled_catchup(harvest_crossref_prefix)
harvest_mountain_wetlands = log_scheduled_catchup(harvest_mountain_wetlands)
harvest_openalex_source = log_scheduled_catchup(harvest_openalex_source)
harvest_geoscienceworld = log_scheduled_catchup(harvest_geoscienceworld)
CACHE_DIR = Path(tempfile.gettempdir()) / "optimap_cache"
User = get_user_model()


def dedup_sweep(locations_only=False, force=False, limit=None):
    """Django-Q entry point: backfill OpenAlex locations + auto-merge duplicates.

    Thin wrapper over ``works.dedup.sweep`` so recurring schedules and the
    ``dedup_works --async`` command can resolve it by dotted path. See
    ``works/dedup.py`` for the two-pass behaviour.
    """
    from works.dedup import sweep

    stats = sweep(locations_only=locations_only, force=force, limit=limit)
    logger.info("dedup_sweep finished: %s", stats)
    return stats


# -----------------------------------------------------------------------------
# Data-dump helpers (used by regenerate_geojson_cache / regenerate_geopackage_cache).
# -----------------------------------------------------------------------------


def generate_data_dump_filename(extension: str) -> str:
    ts = datetime.now(dt_timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"optimap_data_dump_{ts}.{extension}"


def cleanup_old_data_dumps(directory: Path, keep: int):
    """Keep the newest ``keep`` dump cycles, dropping older files.

    Each regen cycle now produces multiple files for the same timestamp
    (``optimap_data_dump_<ts>.geojson`` + ``.geojson.gz`` + ``.gpkg`` +
    ``.csv``). Counting raw files would prune fresh formats from the current
    cycle (e.g. drop ``.csv`` because it sorts after ``.gpkg``); instead, we
    group by the ``optimap_data_dump_<ts>`` prefix and keep the newest
    ``keep`` *cycles*.
    """
    pattern = str(directory / "optimap_data_dump_*")
    files = glob.glob(pattern)
    # Group by `optimap_data_dump_<TS>`. The timestamp is fixed-width
    # (``%Y%m%dT%H%M%S``) so the second underscore-delimited field is the
    # full prefix we want regardless of extension.
    cycles = defaultdict(list)
    for path in files:
        name = os.path.basename(path)
        # `optimap_data_dump_<TS>.<ext>` — split on the first '.' to get the
        # cycle key (drops the extension, including compound `.geojson.gz`).
        cycle_key = name.split(".", 1)[0]
        cycles[cycle_key].append(path)
    for cycle_key in sorted(cycles, reverse=True)[keep:]:
        for old in cycles[cycle_key]:
            try:
                os.remove(old)
            except OSError:
                logger.warning("Could not delete old dump %s", old)


# -----------------------------------------------------------------------------
# Monthly email digest.
# -----------------------------------------------------------------------------


@log_scheduled_catchup
def send_monthly_email(trigger_source="manual", sent_by=None):
    """
    Send the monthly digest of new manuscripts to users who opted in.

    Rules:
      - One email per distinct recipient with a non-empty address.
      - Link for each work:
          * if DOI present  -> prefer OPTIMAP permalink, fallback to https://doi.org/<doi>
          * else            -> fallback to Work.url (may be empty)
      - Log success/failure to EmailLog.
      - Respect settings.EMAIL_SEND_DELAY if present.
    """
    recipients_qs = (
        User.objects.filter(userprofile__notify_new_manuscripts=True)
        .exclude(email__isnull=True)
        .exclude(email__exact="")
        .values_list("email", flat=True)
        .distinct()
    )
    recipients = list(recipients_qs)

    last_month = timezone.now().replace(day=1) - timedelta(days=1)
    new_manuscripts = Work.objects.filter(
        creationDate__year=last_month.year,
        creationDate__month=last_month.month,
    )

    if not recipients or not new_manuscripts.exists():
        return

    def link_for(work):
        if work.doi:
            try:
                permalink = work.permalink()
            except TypeError:
                permalink = work.permalink if hasattr(work, "permalink") else None
            if permalink:
                return permalink
            return f"https://doi.org/{work.doi}"
        return work.url or ""

    manuscripts = [{"title": w.title, "link": link_for(w)} for w in new_manuscripts]
    subject, content = render_email("email/monthly_digest.en.txt", {"manuscripts": manuscripts})

    delay_seconds = getattr(settings, "EMAIL_SEND_DELAY", 0)

    for recipient in recipients:
        try:
            send_mail(
                subject,
                content,
                settings.EMAIL_HOST_USER,
                [recipient],
                fail_silently=False,
            )
            EmailLog.log_email(
                recipient,
                subject,
                content,
                sent_by=sent_by,
                trigger_source=trigger_source,
                status="success",
            )
            if delay_seconds:
                time.sleep(delay_seconds)
        except Exception as e:
            logger.error("Failed to send monthly email to %s: %s", recipient, e)
            EmailLog.log_email(
                recipient,
                subject,
                content,
                sent_by=sent_by,
                trigger_source=trigger_source,
                status="failed",
                error_message=str(e),
            )


def _matching_publications(items, cutoff):
    """Map each subscribed region/source/collection to its matching published
    works added since cutoff (reading persisted Work relations, not
    re-computing them). Drops items with no matches, and items with no
    landing-page URL (e.g. a Source with a still-blank slug) so they can't be
    counted in ``total_publications`` and then silently omitted from the
    email body by ``_build_subscription_groups``."""
    publications = {}
    for item in items:
        if not item.get_absolute_url():
            continue
        matching_pubs = list(item.works.filter(status="p", creationDate__gte=cutoff).order_by("-creationDate")[:50])
        if matching_pubs:
            publications[item] = matching_pubs
    return publications


def _build_subscription_group(obj, pubs, name, url, extra_fields=None):
    """Shape a (region|source|collection, matching works) pair into the dict
    consumed by subscription_regional.en.txt — same shape for every section
    so the email template can loop over all three identically."""
    pub_items = [
        {
            "title": (w.title[:100] + "..." if len(w.title) > 100 else w.title),
            "link": _get_article_link(w),
        }
        for w in pubs[:10]
    ]
    group = {
        "name": name,
        "pub_count": len(pubs),
        "url": url,
        "pubs": pub_items,
        "extra_count": max(0, len(pubs) - 10),
    }
    if extra_fields:
        group.update(extra_fields)
    return group


def _build_subscription_groups(publications, extra_fields=None):
    """Turn a {item: pubs} map (from ``_matching_publications``, which already
    excludes items with no landing-page URL) into a name-sorted list of
    email-template groups."""
    return [
        _build_subscription_group(
            item,
            pubs,
            item.name,
            f"{BASE_URL}{item.get_absolute_url()}",
            extra_fields=extra_fields(item) if extra_fields else None,
        )
        for item, pubs in sorted(publications.items(), key=lambda pair: pair[0].name)
    ]


@log_scheduled_catchup
def send_subscription_based_email(trigger_source="manual", sent_by=None, user_ids=None, interval=None):
    """
    Send subscription-based notifications grouped by region, source, and collection.

    Publications are grouped by the regions/sources/collections the user has
    subscribed to. Each group includes a link to that item's landing page.
    Works may appear in more than one section (e.g. a work matching both a
    subscribed region and a subscribed source) — sections are not deduplicated
    against each other.

    ``interval`` — when given ('weekly' or 'monthly'), only processes subscriptions
    whose ``notification_interval`` matches. Pass ``None`` (the default, used for
    manual/admin runs) to process all active subscriptions regardless of their
    interval setting.

    Only publications added since ``subscription.last_notified`` are included.
    If ``last_notified`` is unset, a sensible fallback window is used (7 days for
    weekly, 31 days for monthly, 31 days when interval is None).
    ``last_notified`` is updated only after a successful send.
    """
    query = Subscription.objects.filter(subscribed=True, user__isnull=False).prefetch_related(
        "regions", "sources", "collections"
    )
    if user_ids:
        query = query.filter(user__id__in=user_ids)
    if interval is not None:
        query = query.filter(notification_interval=interval)

    fallback_days = 7 if interval == "weekly" else 31

    for subscription in query:
        user_email = subscription.user.email

        subscribed_regions = list(subscription.regions.all())
        subscribed_sources = list(subscription.sources.all())
        # Re-check is_published here (not just at subscribe-time in add_subscriptions):
        # a collection can be unpublished after a user already subscribed to it, or
        # be added directly via the admin without going through that guard.
        subscribed_collections = list(subscription.collections.filter(is_published=True))
        if not (subscribed_regions or subscribed_sources or subscribed_collections):
            logger.info(f"Skipping subscription for {user_email} - no regions/sources/collections selected")
            continue

        cutoff = (
            subscription.last_notified
            if subscription.last_notified
            else (timezone.now() - timedelta(days=fallback_days))
        )

        # Read the persisted Work.regions M2M (populated by the
        # assign_work_regions signal / backfill_work_regions sweep) rather
        # than re-intersecting every published work's geometry here.
        region_publications = _matching_publications(subscribed_regions, cutoff)
        source_publications = _matching_publications(subscribed_sources, cutoff)
        collection_publications = _matching_publications(subscribed_collections, cutoff)

        total_publications = sum(
            len(pubs)
            for publications in (region_publications, source_publications, collection_publications)
            for pubs in publications.values()
        )
        if total_publications == 0:
            logger.info(f"Skipping subscription for {user_email} - no new publications since {cutoff}")
            continue

        unsubscribe_all = f"{BASE_URL}{reverse('optimap:unsubscribe')}?all=true"
        manage_subscriptions = f"{BASE_URL}{reverse('optimap:subscriptions')}"

        region_groups = _build_subscription_groups(
            region_publications, extra_fields=lambda r: {"region_type": r.get_region_type_display()}
        )
        source_groups = _build_subscription_groups(source_publications)
        collection_groups = _build_subscription_groups(collection_publications)

        subject, content = render_email(
            "email/subscription_regional.en.txt",
            {
                "total_publications": total_publications,
                "username": subscription.user.username,
                "region_groups": region_groups,
                "source_groups": source_groups,
                "collection_groups": collection_groups,
                "manage_subscriptions": manage_subscriptions,
                "unsubscribe_all": unsubscribe_all,
                "base_url": BASE_URL,
            },
        )

        try:
            email = EmailMessage(subject, content, settings.EMAIL_HOST_USER, [user_email])
            email.send()
            EmailLog.log_email(
                user_email, subject, content, sent_by=sent_by, trigger_source=trigger_source, status="success"
            )
            logger.info(
                f"Sent subscription email to {user_email} with {total_publications} publications across "
                f"{len(region_publications)} regions, {len(source_publications)} sources, "
                f"{len(collection_publications)} collections"
            )
            subscription.last_notified = timezone.now()
            subscription.save(update_fields=["last_notified"])
            time.sleep(settings.EMAIL_SEND_DELAY)
        except Exception as e:
            error_message = str(e)
            logger.error(f"Failed to send subscription email to {user_email}: {error_message}")
            EmailLog.log_email(
                user_email,
                subject,
                content,
                sent_by=sent_by,
                trigger_source=trigger_source,
                status="failed",
                error_message=error_message,
            )


def schedule_monthly_email_task(sent_by=None):
    if not Schedule.objects.filter(func="works.tasks.send_monthly_email").exists():
        now = timezone.localtime()
        last_day_of_month = calendar.monthrange(now.year, now.month)[1]
        next_run_date = now.replace(day=last_day_of_month, hour=23, minute=59)
        # Function kwargs are spread, not passed as kwargs={...}: django_q's
        # schedule() treats leftover keyword args as the task's own kwargs.
        schedule(
            "works.tasks.send_monthly_email",
            schedule_type="M",
            repeats=-1,
            next_run=next_run_date,
            intended_date_kwarg="scheduled_for",
            trigger_source="scheduled",
            sent_by=sent_by.id if sent_by else None,
        )
        logger.info(f"Scheduled 'schedule_monthly_email_task' for {next_run_date}")


def schedule_subscription_email_task(sent_by=None):
    # Monthly subscription digest — distinguished from the weekly one by schedule_type "M".
    if not Schedule.objects.filter(func="works.tasks.send_subscription_based_email", schedule_type="M").exists():
        now = timezone.localtime()
        last_day_of_month = calendar.monthrange(now.year, now.month)[1]
        next_run_date = now.replace(day=last_day_of_month, hour=23, minute=59)
        schedule(
            "works.tasks.send_subscription_based_email",
            schedule_type="M",
            repeats=-1,
            next_run=next_run_date,
            intended_date_kwarg="scheduled_for",
            trigger_source="scheduled",
            interval="monthly",
            sent_by=sent_by.id if sent_by else None,
        )
        logger.info(f"Scheduled monthly 'send_subscription_based_email' for {next_run_date}")


def schedule_weekly_subscription_email_task(sent_by=None):
    # Weekly subscription digest — distinguished from the monthly one by weekly schedule_type "W".
    # Uses "W" (not cron "C") so it works without the optional croniter dependency.
    if not Schedule.objects.filter(func="works.tasks.send_subscription_based_email", schedule_type="W").exists():
        next_run = _next_monday().replace(hour=2, minute=0, second=0, microsecond=0)
        schedule(
            "works.tasks.send_subscription_based_email",
            schedule_type="W",
            repeats=-1,
            next_run=next_run,
            intended_date_kwarg="scheduled_for",
            trigger_source="scheduled",
            interval="weekly",
            sent_by=sent_by.id if sent_by else None,
        )
        logger.info("Scheduled weekly 'send_subscription_based_email' (Mondays 02:00).")


# -----------------------------------------------------------------------------
# Inactivity warning (#120) and deletion list (#121).
# -----------------------------------------------------------------------------


def _next_monday():
    """Return next Monday at 08:00 (always at least 1 day ahead)."""
    now = timezone.now()
    days_ahead = (7 - now.weekday()) % 7 or 7
    return (now + timedelta(days=days_ahead)).replace(hour=8, minute=0, second=0, microsecond=0)


@log_scheduled_catchup
def send_inactivity_warning_emails(trigger_source="scheduled"):
    """Email users in the 12-to-13-month inactivity window (#120)."""
    from django.contrib.auth import get_user_model

    from works.models import EmailLog
    from works.views.auth import is_email_blocked

    User = get_user_model()
    now = timezone.now()
    warning_cutoff = now - timedelta(days=settings.INACTIVITY_WARNING_DAYS)
    deletion_cutoff = now - timedelta(days=settings.INACTIVITY_DELETION_DAYS)

    users = User.objects.filter(
        is_active=True,
        last_login__lt=warning_cutoff,
        last_login__gte=deletion_cutoff,
    ).exclude(email="")

    login_url = settings.BASE_URL

    for user in users:
        if is_email_blocked(user.email):
            logger.info("Skipping blocked address %s for inactivity warning.", user.email)
            continue
        subject, body = render_email(
            "email/account_inactivity_warning.en.txt",
            {
                "email": user.email,
                "login_url": login_url,
            },
        )
        try:
            send_mail(subject, body, settings.EMAIL_HOST_USER, [user.email], fail_silently=False)
            EmailLog.log_email(user.email, subject, body, trigger_source=trigger_source, status="success")
        except Exception as ex:  # noqa: BLE001
            logger.exception("Failed to send inactivity warning to %s.", user.email)
            EmailLog.log_email(
                user.email, subject, body, trigger_source=trigger_source, status="failed", error_message=str(ex)
            )
        time.sleep(settings.EMAIL_SEND_DELAY)


@log_scheduled_catchup
def send_inactivity_deletion_list_to_admins(trigger_source="scheduled"):
    """Email admins a list of users inactive for 13+ months (#121)."""
    from django.contrib.auth import get_user_model

    from works.models import EmailLog

    User = get_user_model()
    now = timezone.now()
    deletion_cutoff = now - timedelta(days=settings.INACTIVITY_DELETION_DAYS)

    stale_users = list(
        User.objects.filter(is_active=True, last_login__lt=deletion_cutoff).exclude(email="").order_by("last_login")
    )
    if not stale_users:
        logger.info("send_inactivity_deletion_list_to_admins: no users pending deletion.")
        return

    admin_emails = list(User.objects.filter(is_staff=True).exclude(email="").values_list("email", flat=True))
    if not admin_emails:
        logger.warning("send_inactivity_deletion_list_to_admins: no admin emails configured.")
        return

    # Attach the date of the most recent successful warning email to each user.
    stale_emails = [u.email for u in stale_users]
    warning_log_by_email = {}
    for log in EmailLog.objects.filter(
        recipient_email__in=stale_emails, subject__icontains="account will be deleted", status="success"
    ).order_by("-sent_at"):
        warning_log_by_email.setdefault(log.recipient_email, log)
    for user in stale_users:
        user.warning_log = warning_log_by_email.get(user.email)

    admin_url = f"{settings.BASE_URL}{reverse('admin:works_customuser_changelist')}"
    subject, body = render_email(
        "email/account_deletion_pending.en.txt",
        {
            "count": len(stale_users),
            "users": stale_users,
            "admin_url": admin_url,
        },
    )
    for admin_email in admin_emails:
        try:
            send_mail(subject, body, settings.EMAIL_HOST_USER, [admin_email], fail_silently=False)
            EmailLog.log_email(admin_email, subject, body, trigger_source=trigger_source, status="success")
        except Exception as ex:  # noqa: BLE001
            logger.exception("Failed to send deletion list to admin %s.", admin_email)
            EmailLog.log_email(
                admin_email, subject, body, trigger_source=trigger_source, status="failed", error_message=str(ex)
            )
        time.sleep(settings.EMAIL_SEND_DELAY)


def schedule_inactivity_warning_task():
    if not Schedule.objects.filter(func="works.tasks.send_inactivity_warning_emails").exists():
        schedule(
            "works.tasks.send_inactivity_warning_emails",
            schedule_type="W",
            repeats=-1,
            next_run=_next_monday(),
            intended_date_kwarg="scheduled_for",
        )
        logger.info("Scheduled send_inactivity_warning_emails weekly.")


def schedule_inactivity_deletion_task():
    if not Schedule.objects.filter(func="works.tasks.send_inactivity_deletion_list_to_admins").exists():
        schedule(
            "works.tasks.send_inactivity_deletion_list_to_admins",
            schedule_type="W",
            repeats=-1,
            next_run=_next_monday(),
            intended_date_kwarg="scheduled_for",
        )
        logger.info("Scheduled send_inactivity_deletion_list_to_admins weekly.")


# -----------------------------------------------------------------------------
# External-service token renewal reminders.
# -----------------------------------------------------------------------------


@log_scheduled_catchup
def check_service_token_renewals(trigger_source="scheduled"):
    """Email staff when any external-service refresh token nears expiry.

    Generic over every service registered in
    ``works.utils.service_tokens.get_service_token_specs`` (OpenAIRE today).
    A refresh token must be rotated about once a month; this weekly check emails
    active staff whenever a token expires within the renewal window (default 9
    days), with a link to the provider docs and step-by-step renewal instructions
    (Django admin — no SSH). When nothing is due it simply logs and returns.
    """
    from works.models import EmailLog, ServiceToken
    from works.utils.service_tokens import get_service_token_specs

    User = get_user_model()
    specs = get_service_token_specs()
    logger.info("check_service_token_renewals: checking %d registered service(s).", len(specs))

    due = []
    for token_row in ServiceToken.objects.filter(service__in=specs.keys()):
        if not token_row.refresh_token or not token_row.refresh_token_set_at:
            continue
        if not token_row.due_for_reminder():
            continue
        spec = specs[token_row.service]
        admin_url = f"{settings.BASE_URL}{reverse(spec.admin_change_viewname, args=[token_row.pk])}"
        due.append(
            {
                "row": token_row,
                "label": spec.label,
                "days_until_expiry": token_row.days_until_refresh_expiry(),
                "expires_at": token_row.refresh_token_expires_at,
                "docs_url": spec.docs_url,
                "token_page_url": spec.token_page_url,
                "admin_url": admin_url,
            }
        )

    if not due:
        logger.info("check_service_token_renewals: no tokens due for renewal.")
        return

    admin_emails = list(
        User.objects.filter(is_staff=True, is_active=True).exclude(email="").values_list("email", flat=True)
    )
    if not admin_emails:
        logger.warning("check_service_token_renewals: no staff emails configured.")
        return

    subject, body = render_email(
        "email/service_token_renewal.en.txt",
        {"count": len(due), "tokens": due},
    )
    sent_any = False
    for admin_email in admin_emails:
        try:
            send_mail(subject, body, settings.EMAIL_HOST_USER, [admin_email], fail_silently=False)
            EmailLog.log_email(admin_email, subject, body, trigger_source=trigger_source, status="success")
            sent_any = True
        except Exception as ex:  # noqa: BLE001
            logger.exception("Failed to send service-token renewal reminder to %s.", admin_email)
            EmailLog.log_email(
                admin_email, subject, body, trigger_source=trigger_source, status="failed", error_message=str(ex)
            )
        time.sleep(settings.EMAIL_SEND_DELAY)

    # Record when each flagged token was last reminded (informational only).
    if sent_any:
        reminded_at = timezone.now()
        for entry in due:
            row = entry["row"]
            row.last_reminder_sent_at = reminded_at
            row.save(update_fields=["last_reminder_sent_at", "updated_at"])
    logger.info("check_service_token_renewals: reminded staff for %d token(s).", len(due))


def schedule_service_token_renewal_check():
    if not Schedule.objects.filter(func="works.tasks.check_service_token_renewals").exists():
        schedule(
            "works.tasks.check_service_token_renewals",
            schedule_type="W",
            repeats=-1,
            next_run=_next_monday(),
            intended_date_kwarg="scheduled_for",
        )
        logger.info("Scheduled check_service_token_renewals weekly.")


# -----------------------------------------------------------------------------
# Country-code backfill sweep (issue #261).
# -----------------------------------------------------------------------------


def backfill_work_countries(trigger_source="scheduled", limit=None, dry_run=False):
    """Link ``Work.countries`` for works that have geometry but no countries yet.

    Self-healing catch-up for the on-save signal: fills works saved with
    geocoding off, harvested before #261, or whose geometry never matched (the
    offline point-in-polygon join is deterministic and cheap, so ocean / no-match
    works are simply retried each run). Uses
    ``works.services.countries.lookup_countries`` — the same helper as the
    post-save signal — so multi-country geometries link every intersecting
    country and the join method (direct vs. snap) is recorded in provenance.

    Emails active staff a summary **only when something changed or errored**
    (silent on no-op runs), following the ``check_service_token_renewals``
    pattern. Returns the tally dict for callers/tests.
    """
    from works.models import Country
    from works.services.countries import lookup_countries
    from works.utils.provenance import set_block

    tally = {"processed": 0, "updated": 0, "multi_country": 0, "no_match": 0, "errors": 0}

    # .real() excludes the always-present sentinel row, so this still detects a
    # Country table with no real countries loaded yet.
    if not Country.objects.real().exists():
        logger.warning("backfill_work_countries: Country table empty — run load_countries first; skipping.")
        return tally

    qs = (
        Work.objects.filter(geometry__isnull=False)
        .exclude(geometry__isempty=True)
        .filter(countries__isnull=True)
        .order_by("id")
    )
    if limit:
        qs = qs[:limit]

    for work in qs.iterator():
        tally["processed"] += 1
        # Intermediate progress so a long sweep over thousands of works is
        # visible in the Q-cluster log rather than going silent until the end.
        if tally["processed"] % 100 == 0:
            logger.info(
                "backfill_work_countries: progress — processed %d, updated %d, no-match %d, errors %d.",
                tally["processed"],
                tally["updated"],
                tally["no_match"],
                tally["errors"],
            )
        try:
            countries, prov = lookup_countries(work.geometry)
        except Exception:  # noqa: BLE001
            logger.exception("backfill_work_countries: lookup failed for work %s.", work.pk)
            tally["errors"] += 1
            continue
        if not countries:
            tally["no_match"] += 1
            continue
        if len(countries) > 1:
            tally["multi_country"] += 1
        if not dry_run:
            work.countries.set(countries)
            # Record how the join was made (direct vs. territorial-sea snap).
            set_block(work, "countries", prov)
        tally["updated"] += 1

    logger.info(
        "backfill_work_countries: processed %d, updated %d (multi %d), no-match %d, errors %d%s.",
        tally["processed"],
        tally["updated"],
        tally["multi_country"],
        tally["no_match"],
        tally["errors"],
        " (dry-run)" if dry_run else "",
    )

    if dry_run or not (tally["updated"] or tally["errors"]):
        return tally

    User = get_user_model()
    admin_emails = list(
        User.objects.filter(is_staff=True, is_active=True).exclude(email="").values_list("email", flat=True)
    )
    if not admin_emails:
        logger.warning("backfill_work_countries: no staff emails configured; skipping notification.")
        return tally

    subject, body = render_email(
        "email/country_backfill.en.txt",
        {"tally": tally, "base_url": settings.BASE_URL},
    )
    for admin_email in admin_emails:
        try:
            send_mail(subject, body, settings.EMAIL_HOST_USER, [admin_email], fail_silently=False)
            EmailLog.log_email(admin_email, subject, body, trigger_source=trigger_source, status="success")
        except Exception as ex:  # noqa: BLE001
            logger.exception("backfill_work_countries: failed to email %s.", admin_email)
            EmailLog.log_email(
                admin_email, subject, body, trigger_source=trigger_source, status="failed", error_message=str(ex)
            )
        time.sleep(settings.EMAIL_SEND_DELAY)

    return tally


def schedule_backfill_work_countries():
    if not Schedule.objects.filter(func="works.tasks.backfill_work_countries").exists():
        schedule(
            "works.tasks.backfill_work_countries",
            schedule_type="W",
            repeats=-1,
            # Offset from the other weekly sweeps so they don't all fire at 08:00.
            next_run=_next_monday().replace(hour=4),
            intended_date_kwarg="scheduled_for",
        )
        logger.info("Scheduled backfill_work_countries weekly.")


def backfill_work_regions(trigger_source="scheduled", limit=None, dry_run=False):
    """Link ``Work.regions`` for works that have geometry but no regions yet.

    The global-region mirror of :func:`backfill_work_countries`: self-healing
    catch-up for the on-save signal, filling works saved with geocoding off,
    harvested before regions were persisted, or whose geometry never matched
    (the offline join is deterministic and cheap, so no-match works are simply
    retried each run). Uses ``works.services.regions.lookup_regions`` — the same
    helper as the post-save signal — so a coastal work links its continent and
    ocean and the join is recorded in provenance.

    Emails active staff a summary **only when something changed or errored**
    (silent on no-op runs). Returns the tally dict for callers/tests.
    """
    from works.models import GlobalRegion
    from works.services.regions import lookup_regions
    from works.utils.provenance import set_block

    tally = {"processed": 0, "updated": 0, "multi_region": 0, "no_match": 0, "errors": 0}

    if not GlobalRegion.objects.exists():
        logger.warning("backfill_work_regions: GlobalRegion table empty — run load_global_regions first; skipping.")
        return tally

    from works.views_regions import NOT_MANUAL_REGION

    qs = (
        Work.objects.filter(geometry__isnull=False)
        .exclude(geometry__isempty=True)
        .filter(regions__isnull=True)
        # Skip works a curator decided manually (assigned a region, or marked
        # "will not be matched" with zero regions) so the sweep does not undo it.
        .filter(NOT_MANUAL_REGION)
        .order_by("id")
    )
    if limit:
        qs = qs[:limit]

    touched_regions = set()
    for work in qs.iterator():
        tally["processed"] += 1
        if tally["processed"] % 100 == 0:
            logger.info(
                "backfill_work_regions: progress — processed %d, updated %d, no-match %d, errors %d.",
                tally["processed"],
                tally["updated"],
                tally["no_match"],
                tally["errors"],
            )
        try:
            regions, prov = lookup_regions(work.geometry)
        except Exception:  # noqa: BLE001
            logger.exception("backfill_work_regions: lookup failed for work %s.", work.pk)
            tally["errors"] += 1
            continue
        if not regions:
            tally["no_match"] += 1
            continue
        if len(regions) > 1:
            tally["multi_region"] += 1
        if not dry_run:
            work.regions.set(regions)
            set_block(work, "regions", prov)
            touched_regions.update(regions)
        tally["updated"] += 1

    if touched_regions:
        from works.views_regions import invalidate_region_page_cache

        for region in touched_regions:
            invalidate_region_page_cache(region)

    logger.info(
        "backfill_work_regions: processed %d, updated %d (multi %d), no-match %d, errors %d%s.",
        tally["processed"],
        tally["updated"],
        tally["multi_region"],
        tally["no_match"],
        tally["errors"],
        " (dry-run)" if dry_run else "",
    )

    if dry_run or not (tally["updated"] or tally["errors"]):
        return tally

    User = get_user_model()
    admin_emails = list(
        User.objects.filter(is_staff=True, is_active=True).exclude(email="").values_list("email", flat=True)
    )
    if not admin_emails:
        logger.warning("backfill_work_regions: no staff emails configured; skipping notification.")
        return tally

    subject, body = render_email(
        "email/region_backfill.en.txt",
        {"tally": tally, "base_url": settings.BASE_URL},
    )
    for admin_email in admin_emails:
        try:
            send_mail(subject, body, settings.EMAIL_HOST_USER, [admin_email], fail_silently=False)
            EmailLog.log_email(admin_email, subject, body, trigger_source=trigger_source, status="success")
        except Exception as ex:  # noqa: BLE001
            logger.exception("backfill_work_regions: failed to email %s.", admin_email)
            EmailLog.log_email(
                admin_email, subject, body, trigger_source=trigger_source, status="failed", error_message=str(ex)
            )
        time.sleep(settings.EMAIL_SEND_DELAY)

    return tally


def schedule_backfill_work_regions():
    if not Schedule.objects.filter(func="works.tasks.backfill_work_regions").exists():
        schedule(
            "works.tasks.backfill_work_regions",
            schedule_type="W",
            repeats=-1,
            # Offset from the country sweep (Mon 04:00) so they don't fire together.
            next_run=_next_monday().replace(hour=5),
            intended_date_kwarg="scheduled_for",
        )
        logger.info("Scheduled backfill_work_regions weekly.")


# -----------------------------------------------------------------------------
# OpenAIRE backfill enrichment.
# -----------------------------------------------------------------------------

# Works missing at least one of the OpenAIRE-fillable target fields.
_OPENAIRE_MISSING_TARGET_FIELD = (
    Q(abstract__isnull=True)
    | Q(abstract="")
    | Q(keywords__isnull=True)
    | Q(keywords=[])
    | Q(authors__isnull=True)
    | Q(authors=[])
)


def enrich_openaire_backfill(
    collection=None,
    doi_prefix=None,
    source=None,
    limit=None,
    throttle=None,
    force=False,
    dry_run=False,
    on_progress=None,
    chunked=False,
    after_id=0,
    carry=None,
):
    """Backfill missing abstract/keywords/authors on existing works from OpenAIRE.

    Shared core for the ``enrich_openaire`` management command: runs synchronously
    when called directly (command's sync path, ``chunked=False`` — interruptible
    with Ctrl-C) and as Django-Q tasks when enqueued with ``--async``
    (``chunked=True``). Selects works by collection / DOI prefix / source (mirrors
    the command flags) and fills empty target fields via ``enrich_work_from_openaire``
    (fill-if-empty; never overwrites).

    When ``chunked``, the task processes works only until a wall-clock budget
    (``OPTIMAP_OPENAIRE_ENRICH_CHUNK_SECONDS``) elapses, then re-enqueues itself with
    an ``after_id`` cursor and the running ``carry`` totals — so each Django-Q task
    stays short and the cluster's stop signal is honoured between chunks.

    After ``OPTIMAP_OPENAIRE_ENRICH_ABORT_AFTER`` *consecutive* systemic OpenAIRE
    failures (:class:`~works.harvesting.openaire.OpenAireUnavailable`) the run emails
    staff and raises, instead of becoming a dead job that spams warnings for hours.

    ``on_progress`` is an optional ``callable(str)`` for per-work lines; the command
    passes ``self.stdout.write`` to preserve its interactive output. When ``None``
    (async path) progress goes to the logger. Returns a summary dict suitable as the
    Django-Q task result.
    """
    from works.harvesting.openaire import (
        OpenAireUnavailable,
        _SystemicFailureBreaker,
        notify_openaire_abort,
        openaire_task_q_options,
    )

    if throttle is None:
        throttle = settings.OPTIMAP_OPENAIRE_ENRICH_THROTTLE

    def report(message):
        if on_progress is not None:
            on_progress(message)
        else:
            logger.info(message)

    base_qs = Work.objects.filter(doi__isnull=False).exclude(doi="")
    if not force:
        base_qs = base_qs.filter(_OPENAIRE_MISSING_TARGET_FIELD)
    if collection:
        base_qs = base_qs.filter(collections__identifier=collection)
    if doi_prefix:
        base_qs = base_qs.filter(doi__startswith=doi_prefix)
    if source:
        source = str(source)
        base_qs = base_qs.filter(source_id=int(source)) if source.isdigit() else base_qs.filter(source__name=source)
    base_qs = base_qs.order_by("id").distinct()

    carry = carry or {}
    processed = carry.get("processed", 0)
    updated = carry.get("updated", 0)
    no_match = carry.get("no_match", 0)
    failed = carry.get("failed", 0)
    total = carry.get("total")
    if total is None:
        total = base_qs.count()
        if limit:
            total = min(total, limit)
    remaining = (limit - processed) if limit else None

    if after_id == 0:
        if dry_run:
            report("DRY RUN — no changes will be saved")
        report(f"Processing {total} work(s) against OpenAIRE...")

    deadline = (time.monotonic() + settings.OPTIMAP_OPENAIRE_ENRICH_CHUNK_SECONDS) if chunked else None
    breaker = _SystemicFailureBreaker(settings.OPTIMAP_OPENAIRE_ENRICH_ABORT_AFTER)
    session = _openaire_session()
    last_id = after_id
    aborted = False
    try:
        for work in base_qs.filter(id__gt=after_id).iterator(chunk_size=50):
            if remaining is not None and remaining <= 0:
                break
            processed += 1
            if remaining is not None:
                remaining -= 1
            doi = work.doi or ""
            try:
                enrich_work_from_openaire(work, session=session, save=not dry_run, raise_on_unavailable=True)
                breaker.ok()
            except OpenAireUnavailable as exc:
                if breaker.systemic(exc):
                    notify_openaire_abort(
                        "backfill", breaker, processed=processed, total=total, trigger_source="manual"
                    )
                    aborted = True
                    last_id = work.id
                    break
                logger.warning("OpenAIRE backfill: systemic failure for work %s (%s): %s", work.id, doi, exc)
                failed += 1
                last_id = work.id
                if throttle:
                    time.sleep(throttle)
                continue
            except Exception as exc:
                logger.warning("OpenAIRE enrich failed for work %s (%s): %s", work.id, doi, exc)
                breaker.ok()
                failed += 1
                last_id = work.id
                if throttle:
                    time.sleep(throttle)
                continue

            provenance = work.provenance if isinstance(work.provenance, dict) else {}
            match = provenance.get("openaire_match") or {}
            if match.get("status") != "matched":
                report(f"  [{work.id}] {doi} — no OpenAIRE match")
                no_match += 1
            else:
                events = provenance.get("events") or []
                filled = (events[-1].get("fields_filled") if events else None) or []
                if filled:
                    report(f"  [{work.id}] {doi} — filled {filled}")
                    updated += 1
                else:
                    report(f"  [{work.id}] {doi} — matched, nothing to fill")

            last_id = work.id
            if chunked and time.monotonic() >= deadline:
                break
            if throttle:
                time.sleep(throttle)
    finally:
        session.close()

    summary = {
        "processed": processed,
        "updated": updated,
        "no_match": no_match,
        "failed": failed,
        "dry_run": dry_run,
        "aborted": aborted,
    }

    if aborted:
        report(f"ABORTED. processed={processed} updated={updated} no_match={no_match} failed={failed}")
        raise OpenAireUnavailable(
            f"OpenAIRE backfill aborted after {breaker.consecutive} consecutive systemic failures",
            status=getattr(breaker.last_error, "status", None),
        )

    more_left = (remaining is None or remaining > 0) and base_qs.filter(id__gt=last_id).exists()
    if chunked and more_left:
        from django_q.tasks import async_task

        async_task(
            "works.tasks.enrich_openaire_backfill",
            collection=collection,
            doi_prefix=doi_prefix,
            source=source,
            limit=limit,
            throttle=throttle,
            force=force,
            dry_run=dry_run,
            chunked=True,
            after_id=last_id,
            carry={**summary, "total": total},
            q_options=openaire_task_q_options(),
        )
        report(
            f"Chunk done. processed={processed} updated={updated} no_match={no_match} "
            f"failed={failed}; continuation enqueued."
        )
        return summary

    report(f"Done. processed={processed} updated={updated} no_match={no_match} failed={failed}")
    return summary


# -----------------------------------------------------------------------------
# Author ORCID backfill (backfill_author_orcids command; sync + --async chunks).
# -----------------------------------------------------------------------------

# HTTP statuses that mean "upstream refused / cannot serve us" (plus any 5xx),
# as opposed to 404 / empty result which mean "not found for this work".
_ORCID_SYSTEMIC_STATUSES = frozenset({401, 403, 408, 429})


class _UpstreamUnavailable(RuntimeError):
    """A *systemic* upstream (OpenAlex/OpenAIRE) failure, carrying the HTTP status.

    Distinguishes "the API is refusing us" (rate-limiting, expired token, 5xx,
    connection error) from "this work simply has no record there". Used to trip
    the shared :class:`~works.harvesting.openaire._SystemicFailureBreaker`.
    """

    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


def _openalex_systemic(exc):
    """Classify an OpenAlex request failure. Returns ``(is_systemic, status)``.

    A missing work (HTTP 404) is a per-work outcome, not a systemic failure; a
    401/403/408/429, any 5xx, or a connection/timeout (no response) means OpenAlex
    is refusing or unable to answer us and should trip the circuit breaker.
    """
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if status is None:
        return True, None
    return (status in _ORCID_SYSTEMIC_STATUSES or status >= 500), status


def notify_backfill_abort(context, breaker, *, processed, total, trigger_source="manual"):
    """Email active staff (and log ERROR) that an author ORCID backfill aborted.

    Mirrors :func:`works.harvesting.openaire.notify_openaire_abort` but for the
    mixed OpenAlex/OpenAIRE ORCID backfill, using a source-neutral template.
    """
    last_error = breaker.last_error
    status = getattr(last_error, "status", None)
    logger.error(
        "Author ORCID backfill (%s) aborted after %d consecutive systemic failures (HTTP %s): %s",
        context,
        breaker.consecutive,
        status if status is not None else "connection error",
        last_error,
    )

    User = get_user_model()
    recipients = list(
        User.objects.filter(is_staff=True, is_active=True).exclude(email="").values_list("email", flat=True)
    )
    if not recipients:
        logger.warning("notify_backfill_abort: no staff emails configured; alert not sent.")
        return

    subject, body = render_email(
        "email/author_orcid_backfill_aborted.en.txt",
        {
            "context": context,
            "consecutive": breaker.consecutive,
            "status": status if status is not None else "connection error",
            "error": str(last_error),
            "processed": processed,
            "total": total,
        },
    )
    for recipient in recipients:
        try:
            send_mail(subject, body, settings.EMAIL_HOST_USER, [recipient], fail_silently=False)
            EmailLog.log_email(recipient, subject, body, trigger_source=trigger_source, status="success")
        except Exception as ex:  # noqa: BLE001
            logger.exception("Failed to send ORCID backfill abort alert to %s.", recipient)
            EmailLog.log_email(
                recipient, subject, body, trigger_source=trigger_source, status="failed", error_message=str(ex)
            )


def backfill_author_orcids_task(
    source="both",
    limit=None,
    force=False,
    dry_run=False,
    throttle=None,
    on_progress=None,
    chunked=False,
    after_id=0,
    carry=None,
):
    """Backfill ``Work.author_links`` with author ORCIDs from OpenAlex and/or OpenAIRE.

    Shared core for the ``backfill_author_orcids`` management command: runs
    synchronously when called directly (command's sync path, ``chunked=False`` —
    interruptible with Ctrl-C) and as Django-Q tasks when enqueued with ``--async``
    (``chunked=True``). Mirrors the OpenAIRE backfill chunk/abort machinery.

    ``source`` is ``openalex`` / ``openaire`` / ``both`` (default): ``both`` links
    OpenAlex authors first, then runs an OpenAIRE *supplement* pass that adds any
    ORCID not yet linked (recording rank-position conflicts in provenance) without
    overwriting existing links. All linking is fill-if-empty unless ``force``.

    When ``chunked``, the task processes works only until a wall-clock budget
    (``OPTIMAP_ORCID_BACKFILL_CHUNK_SECONDS``) elapses, then re-enqueues itself with
    an ``after_id`` cursor and the running ``carry`` totals — so each Django-Q task
    stays short and the cluster's stop signal is honoured between chunks.

    After ``OPTIMAP_ORCID_BACKFILL_ABORT_AFTER`` *consecutive* systemic upstream
    failures the run emails staff (:func:`notify_backfill_abort`) and raises,
    instead of grinding through thousands of doomed requests.

    ``on_progress`` is an optional ``callable(str)`` for per-work lines; the command
    passes ``self.stdout.write``. When ``None`` (async path) progress goes to the
    logger. Returns a summary dict suitable as the Django-Q task result.
    """
    from works.harvesting.openaire import (
        OpenAireUnavailable,
        _author_records_from_openaire,
        _SystemicFailureBreaker,
        openaire_task_q_options,
    )
    from works.openalex_matcher import author_records_from_authorships
    from works.services.authors import link_work_authors

    if throttle is None:
        throttle = settings.OPTIMAP_ORCID_BACKFILL_THROTTLE

    def report(message):
        if on_progress is not None:
            on_progress(message)
        else:
            logger.info(message)

    use_openalex = source in ("openalex", "both")
    use_openaire = source in ("openaire", "both")

    base_qs = Work.objects.filter(Q(status="p") | Q(status="h"))
    if source == "openalex":
        base_qs = base_qs.filter(openalex_id__isnull=False).exclude(openalex_id="")
    elif source == "openaire":
        base_qs = base_qs.filter(doi__isnull=False).exclude(doi="")
    else:  # both
        base_qs = base_qs.filter(Q(openalex_id__isnull=False, openalex_id__gt="") | Q(doi__isnull=False, doi__gt=""))
    if not force:
        base_qs = base_qs.filter(author_links__isnull=True)
    base_qs = base_qs.order_by("id").distinct()

    carry = carry or {}
    processed = carry.get("processed", 0)
    linked = carry.get("linked", 0)
    supplemented = carry.get("supplemented", 0)
    skipped = carry.get("skipped", 0)
    errors = carry.get("errors", 0)
    total = carry.get("total")
    if total is None:
        total = base_qs.count()
        if limit:
            total = min(total, limit)
    remaining = (limit - processed) if limit else None

    if after_id == 0:
        report(f"Works to process: {total} (source={source})")

    # link_work_authors always writes (no save= flag), so honour --dry-run by
    # reporting the candidate count and returning without touching the database.
    if dry_run:
        report("DRY RUN — no writes.")
        return {
            "processed": processed,
            "linked": linked,
            "supplemented": supplemented,
            "skipped": skipped,
            "errors": errors,
            "dry_run": True,
            "aborted": False,
        }

    deadline = (time.monotonic() + settings.OPTIMAP_ORCID_BACKFILL_CHUNK_SECONDS) if chunked else None
    breaker = _SystemicFailureBreaker(settings.OPTIMAP_ORCID_BACKFILL_ABORT_AFTER)
    oa_session = _openalex_session() if use_openalex else None
    aire_session = _openaire_session() if use_openaire else None
    last_id = after_id
    aborted = False

    def trip_or_record(exc):
        """Record a systemic failure. Returns True if the breaker has tripped."""
        nonlocal aborted
        if breaker.systemic(exc):
            notify_backfill_abort("backfill", breaker, processed=processed, total=total, trigger_source="manual")
            aborted = True
            return True
        logger.warning("ORCID backfill: systemic upstream failure for work %s: %s", last_id, exc)
        return False

    try:
        for work in base_qs.filter(id__gt=after_id).iterator(chunk_size=100):
            if remaining is not None and remaining <= 0:
                break
            processed += 1
            if remaining is not None:
                remaining -= 1
            last_id = work.id
            parts = []
            work_linked = 0

            # --- OpenAlex pass ---
            if use_openalex and work.openalex_id:
                try:
                    url = f"https://api.openalex.org/works/{work.openalex_id}"
                    resp = oa_session.get(url, params={"select": "id,authorships"}, timeout=15)
                    resp.raise_for_status()
                    records = author_records_from_authorships(resp.json().get("authorships") or [])
                    work_linked = link_work_authors(work, records, source="openalex", force=force)
                    breaker.ok()
                    parts.append(f"openalex:{work_linked} linked")
                except Exception as exc:
                    is_sys, status = _openalex_systemic(exc)
                    errors += 1
                    parts.append("openalex:error")
                    if is_sys:
                        if trip_or_record(_UpstreamUnavailable(f"OpenAlex: {exc}", status=status)):
                            break
                    else:
                        breaker.ok()

            # --- OpenAIRE pass (supplement in `both` mode, else primary) ---
            if use_openaire and work.doi:
                try:
                    record = fetch_openaire_record(work.doi, session=aire_session, raise_on_unavailable=True)
                    breaker.ok()
                    if record:
                        records = _author_records_from_openaire(record)
                        n = link_work_authors(
                            work, records, source="openaire", force=force, supplement=(source == "both")
                        )
                        parts.append(f"openaire:+{n}" if source == "both" else f"openaire:{n} linked")
                        if n:
                            if source == "both":
                                supplemented += 1
                            else:
                                work_linked += n
                    else:
                        parts.append("openaire:no record")
                except OpenAireUnavailable as exc:
                    errors += 1
                    parts.append("openaire:error")
                    if trip_or_record(exc):
                        break
                except Exception as exc:
                    logger.warning("OpenAIRE ORCID lookup failed for work %s: %s", work.id, exc)
                    breaker.ok()
                    errors += 1
                    parts.append("openaire:error")

            if work_linked:
                linked += 1
            elif not parts:
                skipped += 1

            report(f"  [{processed}/{total}] work {work.id} — {', '.join(parts) or 'skipped'}")

            if chunked and time.monotonic() >= deadline:
                break
            if throttle:
                time.sleep(throttle)
    finally:
        if oa_session is not None:
            oa_session.close()
        if aire_session is not None:
            aire_session.close()

    summary = {
        "processed": processed,
        "linked": linked,
        "supplemented": supplemented,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
        "aborted": aborted,
    }

    if aborted:
        report(f"ABORTED. processed={processed} linked={linked} supplemented={supplemented} errors={errors}")
        raise _UpstreamUnavailable(
            f"Author ORCID backfill aborted after {breaker.consecutive} consecutive systemic failures",
            status=getattr(breaker.last_error, "status", None),
        )

    more_left = (remaining is None or remaining > 0) and base_qs.filter(id__gt=last_id).exists()
    if chunked and more_left:
        from django_q.tasks import async_task

        async_task(
            "works.tasks.backfill_author_orcids_task",
            source=source,
            limit=limit,
            force=force,
            dry_run=dry_run,
            throttle=throttle,
            chunked=True,
            after_id=last_id,
            carry={**summary, "total": total},
            q_options=openaire_task_q_options(),
        )
        report(
            f"Chunk done. processed={processed} linked={linked} supplemented={supplemented} "
            f"errors={errors}; continuation enqueued."
        )
        return summary

    report(
        f"Done. processed={processed} linked={linked} supplemented={supplemented} skipped={skipped} errors={errors}"
    )
    return summary


# -----------------------------------------------------------------------------
# Data dump regeneration.
# -----------------------------------------------------------------------------


def _unwrap_geometry_collection(geom):
    """Unwrap a single-member GeometryCollection to its primitive type.

    Django's GeometryCollectionField always emits a GEOMETRYCOLLECTION wrapper,
    even for a single Point or Polygon.  GIS tools (QGIS, ArcGIS) cannot apply
    default symbology to GEOMETRYCOLLECTION layers, so we strip the wrapper here
    before writing any GeoJSON or GeoPackage export.
    """
    if geom is None or geom.get("type") != "GeometryCollection":
        return geom
    parts = [g for g in (geom.get("geometries") or []) if g is not None]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    types = {g["type"] for g in parts}
    if len(types) == 1:
        base = types.pop()
        multi_map = {"Point": "MultiPoint", "LineString": "MultiLineString", "Polygon": "MultiPolygon"}
        if base in multi_map:
            return {"type": multi_map[base], "coordinates": [g["coordinates"] for g in parts]}
    return geom


_DUMP_FIELDS = [
    "title",
    "type",
    "doi",
    "url",
    "publicationDate",
    "abstract",
    "volume",
    "issue",
    "first_page",
    "last_page",
    "timeperiod_startdate",
    "timeperiod_enddate",
    "authors",
    "keywords",
    "topics",
    "bok_concepts",
    "placename",
    "openalex_id",
    "openalex_open_access_status",
    "openalex_is_retracted",
]


def regenerate_geojson_cache():
    cache_dir = os.path.join(tempfile.gettempdir(), "optimap_cache")
    os.makedirs(cache_dir, exist_ok=True)

    json_filename = generate_data_dump_filename("geojson")
    json_path = os.path.join(cache_dir, json_filename)

    works_qs = annotate_rounded_geometry(
        Work.objects.filter(status="p").select_related("source").prefetch_related("collections", "countries")
    )

    base_url = settings.BASE_URL.rstrip("/")
    features = []
    for w in works_qs:
        props = {field: getattr(w, field) for field in _DUMP_FIELDS}
        props.update(
            {
                "country_codes": w.country_codes,
                "source_name": w.source.name if w.source else None,
                "source_url": f"{base_url}/api/v1/sources/{w.source.pk}/" if w.source else None,
                "collections": [c.identifier for c in w.collections.all()],
            }
        )
        geometry = (
            json.loads(w._rounded_geojson)
            if w._rounded_geojson
            else round_geojson_coordinates(json.loads(w.geometry.geojson))
        )
        features.append(
            {
                "type": "Feature",
                "id": w.pk,
                "properties": props,
                "geometry": _unwrap_geometry_collection(geometry),
            }
        )

    data = {"type": "FeatureCollection", **_GEOJSON_METADATA, "features": features}
    with open(json_path, "w") as f:
        json.dump(data, f, cls=DjangoJSONEncoder)

    gzip_filename = generate_data_dump_filename("geojson.gz")
    gzip_path = os.path.join(cache_dir, gzip_filename)
    with open(json_path, "rb") as fin, gzip.open(gzip_path, "wb") as fout:
        fout.writelines(fin)

    size = os.path.getsize(json_path)
    logger.info("Cached GeoJSON at %s (%d bytes), gzipped at %s", json_path, size, gzip_path)
    cleanup_old_data_dumps(Path(cache_dir), settings.DATA_DUMP_RETENTION)
    return json_path


def convert_geojson_via_gdal(geojson_path, *, fmt, ext, layer_creation_options=None, field_type_map=None):
    """Convert an existing GeoJSON dump to ``fmt`` via the GDAL Python bindings.

    Uses ``osgeo.gdal.VectorTranslate`` (the in-process equivalent of the
    ``ogr2ogr`` CLI) so the conversion does not depend on the ``ogr2ogr`` binary
    being installed and on ``PATH`` — the GDAL bindings are already a hard
    dependency of the app via GeoDjango.

    ``fmt`` is the OGR driver name (e.g. ``"GPKG"``, ``"CSV"``); ``ext`` is the
    file extension used for the output dump filename (e.g. ``"gpkg"``,
    ``"csv"``); ``layer_creation_options`` is a list of ``KEY=VALUE`` strings
    passed via ``-lco``; ``field_type_map`` is a list of ``SRC=DST`` strings
    passed via ``-mapFieldType`` (used to pin field-type conversions the driver
    would otherwise do implicitly, e.g. ``StringList=String(JSON)`` for GPKG).
    Returns the output path or ``None`` if the conversion fails.
    """
    cache_dir = os.path.dirname(geojson_path)
    out_filename = generate_data_dump_filename(ext)
    out_path = os.path.join(cache_dir, out_filename)
    # A plain list of strings passed as ``options=`` is parsed exactly like the
    # ``ogr2ogr`` command-line arguments would be.
    options = ["-f", fmt]
    for opt in layer_creation_options or []:
        options.extend(["-lco", opt])
    for spec in field_type_map or []:
        options.extend(["-mapFieldType", spec])
    try:
        ds = gdal.VectorTranslate(out_path, geojson_path, options=options)
        if ds is None:
            logger.warning("GDAL %s conversion failed: %s", fmt, gdal.GetLastErrorMsg())
            return None
        # Drop the reference so GDAL flushes the dataset to disk.
        ds = None
        logger.info("GDAL %s conversion succeeded: %s (%s)", fmt, out_path, gdal.GetLastErrorMsg())
        return out_path
    except Exception as err:
        logger.warning("GDAL %s conversion failed: %s", fmt, err)
        return None


def convert_geojson_to_geopackage(geojson_path):
    # GPKG has no native list column type. Without an explicit mapping, GDAL
    # auto-downgrades the GeoJSON array fields (authors/keywords/topics/
    # bok_concepts/collections, all StringList) to String(JSON) and warns about
    # it on every run. Pin that same conversion so the output is unchanged but
    # the benign warnings disappear.
    return convert_geojson_via_gdal(
        geojson_path,
        fmt="GPKG",
        ext="gpkg",
        field_type_map=["StringList=String(JSON)"],
    )


def convert_geojson_to_csv(geojson_path):
    # `GEOMETRY=AS_WKT` makes GDAL emit a `WKT` column instead of dropping
    # the geometry — that's the whole point of a CSV export for #206.
    return convert_geojson_via_gdal(
        geojson_path,
        fmt="CSV",
        ext="csv",
        layer_creation_options=["GEOMETRY=AS_WKT"],
    )


def regenerate_geopackage_cache():
    geojson_path = regenerate_geojson_cache()
    cache_dir = Path(geojson_path).parent
    gpkg_path = convert_geojson_to_geopackage(geojson_path)
    cleanup_old_data_dumps(cache_dir, settings.DATA_DUMP_RETENTION)
    return gpkg_path


def regenerate_csv_cache():
    geojson_path = regenerate_geojson_cache()
    cache_dir = Path(geojson_path).parent
    csv_path = convert_geojson_to_csv(geojson_path)
    cleanup_old_data_dumps(cache_dir, settings.DATA_DUMP_RETENTION)
    return csv_path


@log_scheduled_catchup
def regenerate_all_data_dumps():
    """Regenerate GeoJSON + GeoPackage + CSV from a single PostGIS pass.

    Used as the scheduled task (every ``DATA_DUMP_INTERVAL_HOURS`` hours) and
    by the admin "regenerate all data exports now" action. Returns a dict of
    ``{format: path}``; values may be ``None`` if a conversion failed (the
    GeoJSON path is always present — we'd have raised before this point).
    """
    geojson_path = regenerate_geojson_cache()
    cache_dir = Path(geojson_path).parent
    gpkg_path = convert_geojson_to_geopackage(geojson_path)
    csv_path = convert_geojson_to_csv(geojson_path)
    cleanup_old_data_dumps(cache_dir, settings.DATA_DUMP_RETENTION)
    return {"geojson": geojson_path, "gpkg": gpkg_path, "csv": csv_path}


def recompute_statistics_snapshot():
    """Recompute the statistics snapshot and refresh the cache.

    Django-Q entry point for the staff-triggered "Calculate statistics now"
    button (``POST /api/v1/statistics/recompute/``). Runs off the request
    thread so the page stays responsive even as the computation grows. Returns
    the saved ``StatisticsSnapshot``.
    """
    from works.utils.statistics import save_statistics_snapshot, update_statistics_cache

    snapshot = save_statistics_snapshot()
    update_statistics_cache()
    return snapshot
