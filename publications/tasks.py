import logging
logger = logging.getLogger(__name__)

from django_q.models import Schedule
from publications.models import Publication, HarvestingEvent, Source
from bs4 import BeautifulSoup
import json
import xml.dom.minidom
from django.contrib.gis.geos import GEOSGeometry
import requests
from django.core.mail import send_mail, EmailMessage
from django.utils import timezone 
from requests.auth import HTTPBasicAuth
import os
from django.conf import settings
from django.utils.timezone import now
from django.contrib.auth import get_user_model
User = get_user_model()
from .models import EmailLog, Subscription
from datetime import datetime, timedelta
from django.urls import reverse
from urllib.parse import quote
from datetime import datetime
from django_q.tasks import schedule
from django.utils import timezone 
from django_q.tasks import schedule
from django_q.models import Schedule
import time  
import calendar
import re

BASE_URL = settings.BASE_URL

def extract_geometry_from_html(content):
    for tag in content.find_all("meta"):
        if tag.get("name", None) == "DC.SpatialCoverage":
            data = tag.get("content", None)
            try:
                geom = json.loads(data)
                geom_data = geom["features"][0]["geometry"]
                # preparing geometry data in accordance to geos API fields
                type_geom= {'type': 'GeometryCollection'}
                geom_content = {"geometries" : [geom_data]}
                type_geom.update(geom_content)
                geom_data_string= json.dumps(type_geom)
                try :
                    geom_object = GEOSGeometry(geom_data_string) # GeometryCollection object
                    logging.debug('Found geometry: %s', geom_object)
                    return geom_object
                except Exception as e:
                    logger.error("Cannot create geometry from string '%s': %s", geom_data_string, e)
            except ValueError as e:
                logger.error("Error loading JSON from %s: %s", tag.get("name"), e)

def extract_timeperiod_from_html(content):
    period = [None, None]
    for tag in content.find_all("meta"):
        if tag.get("name", None) in ['DC.temporal', 'DC.PeriodOfTime']:
            data = tag.get("content", None)
            period =  data.split("/")
            logging.debug('Found time period: %s', period)
            break;
    # returning arrays for array field in DB
    return [period[0]], [period[1]]

DOI_REGEX = re.compile(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', re.IGNORECASE)

def parse_oai_xml_and_save_publications(content, event):
    existing_urls = set(Publication.objects.values_list('url', flat=True))
    existing_dois = set(Publication.objects.exclude(doi__isnull=True).values_list('doi', flat=True))
    
    try:
        DOMTree = xml.dom.minidom.parseString(content)
    except Exception as e:
        logger.error("Error parsing XML: %s", e)
        return

    collection = DOMTree.documentElement
    records = collection.getElementsByTagName("record")
    
    if not records:
        logger.warning("No articles found in OAI-PMH response!")
        return
    
    for record in records:
        # Initialize defaults so variables are always defined.
        period_start, period_end, geom_object = [], [], None
        
        try:
            def get_text(tag_name):
                nodes = record.getElementsByTagName(tag_name)
                return nodes[0].firstChild.nodeValue.strip() if nodes and nodes[0].firstChild else None
            
            identifier_value = get_text("dc:identifier")
            title_value = get_text("dc:title")
            abstract_text = get_text("dc:description")
            journal_value = get_text("dc:publisher")
            date_value = get_text("dc:date")
            doi_text = None
            doi_nodes = record.getElementsByTagName("dc:identifier")
            for node in doi_nodes:
                if node.firstChild and node.firstChild.nodeValue:
                    candidate = node.firstChild.nodeValue.strip()
                    match = DOI_REGEX.search(candidate)
                    if match:
                        doi_text = match.group(0)
                        break
            
            # Duplicate checking.
            if doi_text and doi_text in existing_dois:
                logger.info("Skipping duplicate publication (DOI): %s", doi_text)
                continue
            if identifier_value in existing_urls:
                logger.info("Skipping duplicate publication (URL): %s", identifier_value)
                continue
            existing_urls.add(identifier_value)
            if doi_text:
                existing_dois.add(doi_text)
            
            # Skip records without a valid URL.
            if not identifier_value or not identifier_value.startswith("http"):
                logger.warning("Skipping record with invalid URL: %s", identifier_value)
                continue
            
            try:
                with requests.get(identifier_value) as resp:
                    resp.raise_for_status()
                    soup = BeautifulSoup(resp.content, "html.parser")
                    try:
                        geom_object = extract_geometry_from_html(soup)
                    except Exception as geo_err:
                        logger.error("Geometry extraction failed for URL %s: %s", identifier_value, geo_err)
                        geom_object = None
                    # Extract temporal metadata.
                    start_time, end_time = extract_timeperiod_from_html(soup)
                    if not start_time or start_dates in ([None], [""]):
                        start_time = []
                    if not end_dates or end_dates in ([None], [""]):
                        end_dates = []
                    period_start, period_end = start_time, end_time
            except Exception as fetch_err:
                logger.error("Error fetching HTML for %s: %s", identifier_value, fetch_err)
                geom_object = None
                period_start, period_end = [], []
            
            publication = Publication(
                title=title_value,
                abstract=abstract_text,
                publicationDate=date_value,
                url=identifier_value,
                doi=doi_text,
                source=journal_value,
                geometry=geom_object,
                timeperiod_startdate=period_start,
                timeperiod_enddate=period_end,
                job=event
            )
            publication.save()
        except Exception as e:
            logger.error("Error parsing record: %s", e)
            continue

def harvest_oai_endpoint(source_id, user=None):
    source = Source.objects.get(id=source_id)
    event = HarvestingEvent.objects.create(source=source, status="in_progress")

    try:
        response = requests.get(source.url_field)
        response.raise_for_status()
        
        parse_oai_xml_and_save_publications(response.content, event)
        
        event.status = "completed"
        event.completed_at = timezone.now()
        event.save()
        
        new_count = Publication.objects.filter(job=event).count()
        spatial_count = Publication.objects.filter(job=event).exclude(geometry__isnull=True).count()
        temporal_count = Publication.objects.filter(job=event).exclude(timeperiod_startdate=[]).count()
        
        subject = f"Harvesting Completed for {source.collection_name}"
        completed_str = event.completed_at.strftime('%Y-%m-%d %H:%M:%S') if event.completed_at else 'N/A'
        message = (
            f"Harvesting job details:\n\n"
            f"Number of added articles: {new_count}\n"
            f"Number of articles with spatial metadata: {spatial_count}\n"
            f"Number of articles with temporal metadata: {temporal_count}\n"
            f"Collection used: {source.collection_name or 'N/A'}\n"
            f"Journal: {source.url_field}\n"
            f"Job started at: {event.started_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Job completed at: {completed_str}\n"
        )
        
        if user and user.email:
            send_mail(
                subject,
                message,
                settings.EMAIL_HOST_USER,
                [user.email],
                fail_silently=False,
            )
    
    except Exception as e:
        logger.error("Harvesting failed for source %s: %s", source.url_field, str(e))
        event.status = "failed"
        event.completed_at = timezone.now()
        event.save()

def send_monthly_email(trigger_source='manual', sent_by=None):
    recipients = User.objects.filter(userprofile__notify_new_manuscripts=True).values_list('email', flat=True)
    last_month = now().replace(day=1) - timedelta(days=1)
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
                    # publicationDate__gte=subscription.timeperiod_startdate, 
                    # publicationDate__lte=subscription.timeperiod_enddate  
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

def schedule_monthly_email_task(sent_by=None):
    if not Schedule.objects.filter(func='publications.tasks.send_monthly_email').exists():
        now = datetime.now()
        last_day_of_month = calendar.monthrange(now.year, now.month)[1]  # Get last day of the month
        next_run_date = now.replace(day=last_day_of_month, hour=23, minute=59)  # Run at the end of the last day
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
        last_day_of_month = calendar.monthrange(now.year, now.month)[1]  # Get last day of the month
        next_run_date = now.replace(day=last_day_of_month, hour=23, minute=59)  # Run at the end of the last day
        schedule(
            'publications.tasks.send_subscription_based_email',
            schedule_type='M',
            repeats=-1,
            next_run=next_run_date,
            kwargs={'trigger_source': 'scheduled', 'sent_by': sent_by.id if sent_by else None} 
        )
        logger.info(f"Scheduled 'send_subscription_based_email' for {next_run_date}")

