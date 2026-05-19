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
import logging
import os
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.core.mail import EmailMessage, send_mail
from django.core.serializers import serialize
from django.urls import reverse
from django.utils import timezone
from django_q.models import Schedule
from django_q.tasks import schedule

from works.models import EmailLog, Subscription, Work

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
    harvest_crossref_prefix,
    parse_crossref_response_and_save_works,
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
    _short_body,
)

from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)
BASE_URL = settings.BASE_URL
CACHE_DIR = Path(tempfile.gettempdir()) / 'optimap_cache'
User = get_user_model()


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
        User.objects
        .filter(userprofile__notify_new_manuscripts=True)
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

    lines = [f"- {work.title}: {link_for(work)}" for work in new_manuscripts]
    content = "Here are the new manuscripts:\n" + "\n".join(lines)
    subject = "📚 New manuscripts on OPTIMAP"

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


def send_subscription_based_email(trigger_source='manual', sent_by=None, user_ids=None):
    """
    Send subscription-based notifications grouped by region.

    Publications are grouped by the regions the user has subscribed to.
    Each region group includes a link to the region's landing page.
    """
    query = Subscription.objects.filter(subscribed=True, user__isnull=False).prefetch_related('regions')
    if user_ids:
        query = query.filter(user__id__in=user_ids)

    for subscription in query:
        user_email = subscription.user.email

        subscribed_regions = list(subscription.regions.all())
        if not subscribed_regions:
            logger.info(f"Skipping subscription for {user_email} - no regions selected")
            continue

        region_publications = defaultdict(list)
        total_publications = 0

        for region in subscribed_regions:
            prepared_geom = region.geom.prepared

            candidates = Work.objects.filter(
                status="p",
                geometry__isnull=False,
                geometry__bboverlaps=region.geom,
            ).order_by('-creationDate')[:50]

            matching_pubs = [
                work for work in candidates
                if prepared_geom.intersects(work.geometry)
            ]

            if matching_pubs:
                region_publications[region] = matching_pubs
                total_publications += len(matching_pubs)

        if total_publications == 0:
            logger.info(f"Skipping subscription for {user_email} - no new publications")
            continue

        unsubscribe_all = f"{BASE_URL}{reverse('optimap:unsubscribe')}?all=true"
        manage_subscriptions = f"{BASE_URL}{reverse('optimap:subscriptions')}"

        subject = f"🌍 {total_publications} New Publications in Your Subscribed Regions"

        content_lines = [
            f"Dear {subscription.user.username},",
            "",
            f"You have {total_publications} new work(s) in your subscribed regions:",
            ""
        ]

        for region in sorted(region_publications.keys(), key=lambda r: r.name):
            pubs = region_publications[region]
            region_url = f"{BASE_URL}{region.get_absolute_url()}"
            region_type = region.get_region_type_display()

            content_lines.append(f"📍 {region.name} ({region_type}) - {len(pubs)} work(s)")
            content_lines.append(f"   View all publications in this region: {region_url}")
            content_lines.append("")

            for work in pubs[:10]:
                link = _get_article_link(work)
                title = work.title[:100] + "..." if len(work.title) > 100 else work.title
                content_lines.append(f"   • {title}")
                content_lines.append(f"     {link}")
                content_lines.append("")

            if len(pubs) > 10:
                content_lines.append(f"   ... and {len(pubs) - 10} more in {region.name}")
                content_lines.append(f"   View all: {region_url}")
                content_lines.append("")

        content_lines.extend([
            "───────────────────────────────────────",
            "",
            "Manage your regional subscriptions:",
            f"  {manage_subscriptions}",
            "",
            "Unsubscribe from all notifications:",
            f"  {unsubscribe_all}",
            "",
            "---",
            "OPTIMAP - Open Platform for Geospatial Manuscripts",
            f"{BASE_URL}"
        ])

        content = "\n".join(content_lines)

        try:
            email = EmailMessage(subject, content, settings.EMAIL_HOST_USER, [user_email])
            email.send()
            EmailLog.log_email(user_email, subject, content, sent_by=sent_by, trigger_source=trigger_source, status="success")
            logger.info(f"Sent regional subscription email to {user_email} with {total_publications} publications across {len(region_publications)} regions")
            time.sleep(settings.EMAIL_SEND_DELAY)
        except Exception as e:
            error_message = str(e)
            logger.error(f"Failed to send subscription email to {user_email}: {error_message}")
            EmailLog.log_email(user_email, subject, content, sent_by=sent_by, trigger_source=trigger_source, status="failed", error_message=error_message)


def schedule_monthly_email_task(sent_by=None):
    if not Schedule.objects.filter(func='publications.tasks.send_monthly_email').exists():
        now = datetime.now()
        last_day_of_month = calendar.monthrange(now.year, now.month)[1]
        next_run_date = now.replace(day=last_day_of_month, hour=23, minute=59)
        schedule(
            'publications.tasks.send_monthly_email',
            schedule_type='M',
            repeats=-1,
            next_run=next_run_date,
            kwargs={'trigger_source': 'scheduled', 'sent_by': sent_by.id if sent_by else None}
        )
        logger.info(f"Scheduled 'schedule_monthly_email_task' for {next_run_date}")


def schedule_subscription_email_task(sent_by=None):
    if not Schedule.objects.filter(func='publications.tasks.send_subscription_based_email').exists():
        now = datetime.now()
        last_day_of_month = calendar.monthrange(now.year, now.month)[1]
        next_run_date = now.replace(day=last_day_of_month, hour=23, minute=59)
        schedule(
            'publications.tasks.send_subscription_based_email',
            schedule_type='M',
            repeats=-1,
            next_run=next_run_date,
            kwargs={'trigger_source': 'scheduled', 'sent_by': sent_by.id if sent_by else None}
        )
        logger.info(f"Scheduled 'send_subscription_based_email' for {next_run_date}")


# -----------------------------------------------------------------------------
# Data dump regeneration.
# -----------------------------------------------------------------------------

def regenerate_geojson_cache():
    cache_dir = os.path.join(tempfile.gettempdir(), "optimap_cache")
    os.makedirs(cache_dir, exist_ok=True)

    json_filename = generate_data_dump_filename("geojson")
    json_path = os.path.join(cache_dir, json_filename)
    with open(json_path, 'w') as f:
        serialize(
            'geojson',
            Work.objects.filter(status="p"),
            geometry_field='geometry',
            srid=4326,
            stream=f
        )

    gzip_filename = generate_data_dump_filename("geojson.gz")
    gzip_path = os.path.join(cache_dir, gzip_filename)
    with open(json_path, 'rb') as fin, gzip.open(gzip_path, 'wb') as fout:
        fout.writelines(fin)

    size = os.path.getsize(json_path)
    logger.info("Cached GeoJSON at %s (%d bytes), gzipped at %s", json_path, size, gzip_path)
    cleanup_old_data_dumps(Path(cache_dir), settings.DATA_DUMP_RETENTION)
    return json_path


def convert_geojson_via_ogr(geojson_path, *, fmt, ext, layer_creation_options=None):
    """Convert an existing GeoJSON dump to ``fmt`` via ``ogr2ogr``.

    ``fmt`` is the OGR driver name (e.g. ``"GPKG"``, ``"CSV"``); ``ext`` is the
    file extension used for the output dump filename (e.g. ``"gpkg"``,
    ``"csv"``); ``layer_creation_options`` is a list of ``KEY=VALUE`` strings
    passed via ``-lco``. Returns the output path or ``None`` if ogr2ogr fails.
    """
    cache_dir = os.path.dirname(geojson_path)
    out_filename = generate_data_dump_filename(ext)
    out_path = os.path.join(cache_dir, out_filename)
    cmd = ["ogr2ogr", "-f", fmt, out_path, geojson_path]
    for opt in layer_creation_options or []:
        cmd.extend(["-lco", opt])
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        logger.info("ogr2ogr output (%s):\n%s", fmt, output)
        return out_path
    except subprocess.CalledProcessError as err:
        logger.warning("ogr2ogr %s conversion failed: %s", fmt, err.output)
        return None


def convert_geojson_to_geopackage(geojson_path):
    return convert_geojson_via_ogr(geojson_path, fmt="GPKG", ext="gpkg")


def convert_geojson_to_csv(geojson_path):
    # `GEOMETRY=AS_WKT` makes ogr2ogr emit a `WKT` column instead of dropping
    # the geometry — that's the whole point of a CSV export for #206.
    return convert_geojson_via_ogr(
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


# -----------------------------------------------------------------------------
# Zenodo deposition.
# -----------------------------------------------------------------------------

def run_zenodo_deposition():
    """Run the full Zenodo deposition cycle: regenerate dumps → render
    README/zip/metadata → upload to (or bootstrap) a Zenodo draft.

    Used as the scheduled Django-Q task (annual, last day of the year via
    ``schedule_zenodo_deposit``). Publishing remains manual — admins receive
    an email with the draft link.
    """
    from works.zenodo import deposit_to_zenodo, render_zenodo_package

    regenerate_all_data_dumps()
    render_zenodo_package()
    return deposit_to_zenodo()
