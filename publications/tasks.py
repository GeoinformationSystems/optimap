import logging
logger = logging.getLogger(__name__)

from django_q.models import Schedule
from publications.models import Publication
from bs4 import BeautifulSoup
import json
import xml.dom.minidom
from django.contrib.gis.geos import GEOSGeometry
import requests
from django.core.mail import send_mail
from django.conf import settings
from django.utils.timezone import now
from django.contrib.auth.models import User
from .models import SentEmailLog
from datetime import timedelta


def extract_geometry_from_html(content):
    for tag in content.find_all("meta"):
        if tag.get("name", None) == "DC.SpatialCoverage":
            data = tag.get("content", None)
            try:
                geom = json.loads(data)

                geom_data = geom["features"][0]["geometry"]
                # preparing geometry data in accordance to geosAPI fields
                type_geom= {'type': 'GeometryCollection'}
                geom_content = {"geometries" : [geom_data]}
                type_geom.update(geom_content)
                geom_data_string= json.dumps(type_geom)
                try :
                    geom_object = GEOSGeometry(geom_data_string) # GeometryCollection object
                    logging.debug('Found geometry: %s', geom_object)
                    return geom_object
                except :
                    print("Invalid Geometry")
            except ValueError as e:
                print("Not a valid GeoJSON")

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
        if identifier_value.startswith('http'):

            with requests.get(identifier_value) as response:
                soup = BeautifulSoup(response.content, 'html.parser')

                geom_object = extract_geometry_from_html(soup)
                period_start, period_end = extract_timeperiod_from_html(soup)

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
            journal = journal_value,
            geometry = geom_object,
            timeperiod_startdate = period_start,
            timeperiod_enddate = period_end)
        publication.save()
        logger.info('Saved new publication for %s: %s', identifier_value, publication)

def harvest_oai_endpoint(url):
    try:
        with requests.Session() as s:
            response = s.get(url)
            parse_oai_xml_and_save_publications(response.content)
    except requests.exceptions.RequestException as e:
        print ("The requested URL is invalid or has bad connection.Please change the URL")

def send_monthly_email(sent_by=None):
    recipients = User.objects.values_list('email', flat=True)
    last_month = now().replace(day=1) - timedelta(days=1)  # Get last month's last day
    new_manuscripts = Publication.objects.filter(creationDate__month=last_month.month)

    if not new_manuscripts.exists():
        print("No new manuscripts found for this month. Skipping email.")
        return

    subject = "New Manuscripts This Month"
    content = "Here are the new manuscripts:\n" + "\n".join([pub.title for pub in new_manuscripts])

    for recipient in recipients:
        print(f"Sending email to {recipient}")
        try:
            send_mail(
                subject,
                content,
                settings.EMAIL_HOST_USER,
                [recipient],
                fail_silently=False,
            )
            SentEmailLog.log_email(recipient, subject, content, sent_by=sent_by)
            print(f"Email sent successfully to {recipient}")
        except Exception as e:
            print(f"Failed to send email to {recipient}: {e}")

def schedule_monthly_email_task():
    schedule(
        'publications.tasks.send_monthly_email',
        schedule_type='MONTHLY',
        repeats=-1,
        next_run=datetime.now().replace(day=28, hour=23, minute=59)
    )