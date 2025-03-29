import logging
logger = logging.getLogger(__name__)

from django_q.models import Schedule
from publications.models import Publication
from bs4 import BeautifulSoup
import json
import xml.dom.minidom
from django.contrib.gis.geos import GEOSGeometry
import requests
from django.core.mail import send_mail, EmailMessage
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

def parse_oai_xml_and_save_publications(content):
    DOMTree = xml.dom.minidom.parseString(content)
    collection = DOMTree.documentElement # pass DOMTree as argument
    articles = collection.getElementsByTagName("dc:identifier")
    articles_count_in_journal = len(articles)
    for i in range(articles_count_in_journal):
        identifier = collection.getElementsByTagName("dc:identifier")
        identifier_value = identifier[i].firstChild.nodeValue
        logger.debug("Retrieving %s", identifier_value)
        
        if identifier_value.startswith('http'):

            try:
                with requests.get(identifier_value) as response:
                    soup = BeautifulSoup(response.content, 'html.parser')

                    geom_object = extract_geometry_from_html(soup)
                    period_start, period_end = extract_timeperiod_from_html(soup)
            except Exception as e:
                logger.error("Error retrieving and extracting geometadata from URL %s: %s", identifier_value, e)
                logger.error("Continueing with the next article...")
                continue

        else:
            geom_object = None
            period_start = []
            period_end = []

        title = collection.getElementsByTagName("dc:title")
        if title:
            title_value = title[0].firstChild.nodeValue
        else :
            title_value = None
        abstract = collection.getElementsByTagName("dc:description")
        if abstract:
            abstract_text = abstract[0].firstChild.nodeValue
        else:
            abstract_text = None
        journal = collection.getElementsByTagName("dc:publisher")
        if journal:
            journal_value = journal[0].firstChild.nodeValue
        else:
            journal_value = None
        date = collection.getElementsByTagName("dc:date")
        if date:
            date_value = date[0].firstChild.nodeValue
        else:
            date_value = None

        publication = Publication(
            title = title_value,
            abstract = abstract_text,
            publicationDate = date_value,
            url = identifier_value,
            source = journal_value,
            geometry = geom_object,
            timeperiod_startdate = period_start,
            timeperiod_enddate = period_end)
        publication.save()
        logger.info('Saved new publication for %s: %s', identifier_value, publication.get_absolute_url())

def harvest_oai_endpoint(url):
    try:
        with requests.Session() as s:
            response = s.get(url)
            parse_oai_xml_and_save_publications(response.content)
    except requests.exceptions.RequestException as e:
        logger.error("The requested URL is invalid or has bad connection. Please check the URL: %s", url)

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
            logger.error(f"Failed to send monthly email to {user_email}: {error_message}")
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

        unsubscribe_specific = f"{BASE_URL}{reverse('optimap:unsubscribe')}?search={quote(subscription.search_term)}" # TODO: Change base_url to actual URL
        unsubscribe_all = f"{BASE_URL}{reverse('optimap:unsubscribe')}?all=true"

        subject = f"📚 New Manuscripts Matching '{subscription.search_term}'"

        content = f"""
Dear {subscription.user.username},

Here are the latest manuscripts matching your subscription:

{"\n".join([f"- {pub.title}" for pub in new_publications])}

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

def schedule_monthly_email_task():
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

def schedule_subscription_email_task():
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

