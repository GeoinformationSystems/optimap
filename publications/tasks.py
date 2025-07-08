import logging
logger = logging.getLogger(__name__)

import os
import json
import subprocess
import gzip
import re
import tempfile
import glob
import time
import calendar
from datetime import datetime, timedelta, timezone as dt_timezone
import xml.dom.minidom
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import quote
from django.conf import settings
from django.core.serializers import serialize
from django.core.mail import send_mail, EmailMessage
from django.contrib.gis.geos import GEOSGeometry
from django.utils import timezone
from django_q.tasks import schedule
from django_q.models import Schedule
from publications.models import Publication, HarvestingEvent, Source
from .models import EmailLog, Subscription
from django.contrib.auth import get_user_model
from django.urls import reverse
from geopy.geocoders import Nominatim
from django.contrib.gis.geos import Point

User = get_user_model()

BASE_URL = settings.BASE_URL
DOI_REGEX = re.compile(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', re.IGNORECASE)
CACHE_DIR = Path(tempfile.gettempdir()) / 'optimap_cache'


def generate_data_dump_filename(extension: str) -> str:
    ts = datetime.now(dt_timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"optimap_data_dump_{ts}.{extension}"


def cleanup_old_data_dumps(directory: Path, keep: int):
    """
    Deletes all files matching optimap_data_dump_* beyond the newest `keep` ones.
    """
    pattern = str(directory / "optimap_data_dump_*")
    files = sorted(glob.glob(pattern), reverse=True)
    for old in files[keep:]:
        try:
            os.remove(old)
        except OSError:
            logger.warning("Could not delete old dump %s", old)


def extract_geometry_from_html(soup: BeautifulSoup):
    for tag in soup.find_all("meta"):
        if tag.get("name") == "DC.SpatialCoverage":
            try:
                geom = json.loads(tag["content"])
                geom_data = geom["features"][0]["geometry"]
                coll = {"type": "GeometryCollection", "geometries": [geom_data]}
                return GEOSGeometry(json.dumps(coll))
            except Exception:
                pass
    return None


def extract_timeperiod_from_html(soup: BeautifulSoup):
    for tag in soup.find_all("meta"):
        if tag.get("name") in ("DC.temporal", "DC.PeriodOfTime"):
            parts = tag["content"].split("/")
            start = parts[0] if parts[0] else None
            end   = parts[1] if len(parts) > 1 and parts[1] else None
            return ([start] if start else [None]), ([end] if end else [None]) # If missing, return [None] for start and [None] for end
    return [None], [None]

def parse_oai_xml_and_save_publications(content: bytes, event: HarvestingEvent) -> tuple[int, int, int]:
    """
    Parse OAI-PMH XML, save Publication records linked to `event`,
    and return counts: (added, spatial, temporal).
    """
    try:
        dom = xml.dom.minidom.parseString(content)
    except Exception as e:
        logger.error("Error parsing XML: %s", e)
        return 0, 0, 0

    for record in dom.getElementsByTagName("record"):
        try:
            def get_text(tag_name: str) -> str | None:
                nodes = record.getElementsByTagName(tag_name)
                return (
                    nodes[0].firstChild.nodeValue.strip()
                    if nodes and nodes[0].firstChild else None
                )

            ids = [
                n.firstChild.nodeValue.strip()
                for n in record.getElementsByTagName("dc:identifier")
                if n.firstChild
            ]
            http_ids = [u for u in ids if u.lower().startswith("http")]
            identifier = None
            for u in http_ids:
                if "/view/" in u:
                    identifier = u
                    break
            if not identifier and http_ids:
                identifier = http_ids[0]

            title          = get_text("dc:title")
            abstract       = get_text("dc:description")
            publisher_name = get_text("dc:publisher")
            pub_date       = get_text("dc:date")

            doi = None
            for u in ids:
                m = DOI_REGEX.search(u)
                if m:
                    doi = m.group(0)
                    break

            if doi and Publication.objects.filter(doi=doi).exists():
                continue
            if identifier and Publication.objects.filter(url=identifier).exists():
                continue
            if not identifier or not identifier.startswith("http"):
                continue

            src = None
            if publisher_name:
                src, _ = Source.objects.get_or_create(name=publisher_name)

            geom = None
            ps_list = [None]
            pe_list = [None]
            
            try:
                resp = requests.get(identifier, timeout=10)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.content, "html.parser")

                ps_list, pe_list = extract_timeperiod_from_html(soup)

                g = extract_geometry_from_html(soup)
                if g:
                    geom = g
            
                if src and getattr(src, "is_preprint", False) and geom.empty:
                    try:
                        loc = Nominatim(user_agent="optimap-tasks").geocode(src.homepage_url or src.url)
                        if loc:
                            geom = Point(loc.longitude, loc.latitude)
                    except Exception as e:
                        logger.debug(
                            "Preprint geocode failed for %s: %s",
                            src.name if src else identifier,
                            e
                        )
            except Exception as e:
                logger.debug(
                    "Retrieval and metadata extraction failed for %s: %s",
                    src.name if src else identifier,
                    e
                )
                pass

            Publication.objects.create(
                title=title,
                abstract=abstract,
                publicationDate=pub_date,
                url=identifier,
                doi=doi,
                source=src,
                geometry=geom,
                timeperiod_startdate=ps_list,
                timeperiod_enddate=pe_list,
                job=event,
            )
        except Exception as e:
            logger.error("Error parsing record: %s", e)
            continue

    added_count    = Publication.objects.filter(job=event).count()
    spatial_count  = Publication.objects.filter(job=event).exclude(geometry__isnull=True).count()
    temporal_count = Publication.objects.filter(job=event).exclude(timeperiod_startdate=[]).count()
    return added_count, spatial_count, temporal_count

def harvest_oai_endpoint(source_id: int, user=None) -> None:
    """
    Fetch OAI-PMH feed (HTTP or file://), create a HarvestingEvent,
    parse & save publications, send summary email, and mark completion.
    """
    try:
        src = Source.objects.get(pk=source_id)
    except Source.DoesNotExist:
        logger.error("Source with id %s not found", source_id)
        return
    if src.url_field.startswith("file://"):
        path = src.url_field[7:]
        try:
            with open(path, "rb") as f:
                content = f.read()
        except Exception as e:
            logger.error("Failed to read local file %s: %s", path, e)
            return
    else:
        try:
            resp = requests.get(src.url_field, timeout=30)
            resp.raise_for_status()
            content = resp.content
        except Exception as e:
            logger.error("Harvesting failed for %s: %s", src.url_field, e)
            return

    low = (src.homepage_url or src.url_field or "").lower()
    if any(x in low for x in ("arxiv.org", "biorxiv.org")) and not src.is_preprint:
        src.is_preprint = True
        src.save(update_fields=["is_preprint"])

    event = HarvestingEvent.objects.create(
        source=src,
        user=user,
        status="in_progress",
    )
    added, spatial, temporal = parse_oai_xml_and_save_publications(content, event)
    if user:
        subject = "Harvesting Completed"
        body = (
            f"Collection: {src.collection_name}\n"
            f"Source URL: {src.url_field}\n"
            f"Number of added articles: {added}\n"
            f"Number of articles with spatial metadata: {spatial}\n"
            f"Number of articles with temporal metadata: {temporal}\n"
            f"Harvest started : {event.started_at:%Y-%m-%d}\n"
        )
        send_mail(subject, body, settings.EMAIL_HOST_USER, [user.email])

    event.status       = "completed"
    event.completed_at = timezone.now()
    event.save()

    return added, spatial, temporal


def send_monthly_email(trigger_source='manual', sent_by=None):
    recipients = User.objects.filter(userprofile__notify_new_manuscripts=True).values_list('email', flat=True)
    last_month = timezone.now().replace(day=1) - timedelta(days=1)
    new_manuscripts = Publication.objects.filter(creationDate__month=last_month.month)

    if not recipients.exists() or not new_manuscripts.exists():
        return

    subject = "📚 New Manuscripts This Month"
    content = "Here are the new manuscripts:\n" + "\n".join([pub.title for pub in new_manuscripts])

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
                recipient, subject, content, sent_by=sent_by, trigger_source=trigger_source, status="success"
            )
            time.sleep(settings.EMAIL_SEND_DELAY)
        except Exception as e:
            error_message = str(e)
            logger.error(f"Failed to send monthly email to {recipient}: {error_message}")
            EmailLog.log_email(
                recipient, subject, content, sent_by=sent_by, trigger_source=trigger_source, status="failed", error_message=error_message
            )

def send_subscription_based_email(trigger_source='manual', sent_by=None, user_ids=None):
    query = Subscription.objects.filter(subscribed=True, user__isnull=False)
    if user_ids:
        query = query.filter(user__id__in=user_ids)

    for subscription in query:
        user_email = subscription.user.email

        new_publications = Publication.objects.filter(
            geometry__intersects=subscription.region,
        )

        if not new_publications.exists():
            continue

        unsubscribe_specific = f"{BASE_URL}{reverse('optimap:unsubscribe')}?search={quote(subscription.search_term)}"
        unsubscribe_all = f"{BASE_URL}{reverse('optimap:unsubscribe')}?all=true"

        subject = f"📚 New Manuscripts Matching '{subscription.search_term}'"
        bullet_list = "\n".join([f"- {pub.title}" for pub in new_publications])
        content = f"""Dear {subscription.user.username},
Here are the latest manuscripts matching your subscription:

{bullet_list}

Manage your subscriptions:
Unsubscribe from '{subscription.search_term}': {unsubscribe_specific}
Unsubscribe from All: {unsubscribe_all}
"""

        try:
            email = EmailMessage(subject, content, settings.EMAIL_HOST_USER, [user_email])
            email.send()
            EmailLog.log_email(
                user_email, subject, content, sent_by=sent_by, trigger_source=trigger_source, status="success"
            )
            time.sleep(settings.EMAIL_SEND_DELAY)
        except Exception as e:
            error_message = str(e)
            logger.error(f"Failed to send subscription email to {user_email}: {error_message}")
            EmailLog.log_email(
                user_email, subject, content, sent_by=sent_by, trigger_source=trigger_source, status="failed", error_message=error_message
            )

# ... (the rest of the file remains unchanged)

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


def regenerate_geojson_cache():
    cache_dir = os.path.join(tempfile.gettempdir(), "optimap_cache")
    os.makedirs(cache_dir, exist_ok=True)

    json_filename = generate_data_dump_filename("geojson")
    json_path = os.path.join(cache_dir, json_filename)   
    with open(json_path, 'w') as f:
        serialize(
            'geojson',
            Publication.objects.filter(status="p"),
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
    # remove old dumps beyond retention
    cleanup_old_data_dumps(Path(cache_dir), settings.DATA_DUMP_RETENTION)
    return json_path


def convert_geojson_to_geopackage(geojson_path):
    cache_dir = os.path.dirname(geojson_path)
    gpkg_filename = generate_data_dump_filename("gpkg")
    gpkg_path = os.path.join(cache_dir, gpkg_filename)    
    try:
        output = subprocess.check_output(
            ["ogr2ogr", "-f", "GPKG", gpkg_path, geojson_path],
            stderr=subprocess.STDOUT,
            text=True,
        )
        logger.info("ogr2ogr output:\n%s", output)
        return gpkg_path
    except subprocess.CalledProcessError:
        return None


def regenerate_geopackage_cache():
    geojson_path = regenerate_geojson_cache()
    cache_dir = Path(geojson_path).parent
    gpkg_path = convert_geojson_to_geopackage(geojson_path)
    cleanup_old_data_dumps(cache_dir, settings.DATA_DUMP_RETENTION)
    return gpkg_path