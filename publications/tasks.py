import logging
logger = logging.getLogger(__name__)

from django_q.models import Schedule
from publications.models import Publication
from bs4 import BeautifulSoup
import json
import xml.dom.minidom
from django.contrib.gis.geos import GEOSGeometry
import requests

import os
import subprocess
import gzip
import io
from django.conf import settings
from django_q.tasks import schedule, Schedule
from django.core.serializers import serialize


def extract_geometry_from_html(content):
    for tag in content.find_all("meta"):
        if tag.get("name", None) == "DC.SpatialCoverage":
            data = tag.get("content", None)
            try:
                geom = json.loads(data)
                geom_data = geom["features"][0]["geometry"]
                # preparing geometry data in accordance to geosAPI fields
                type_geom= {'type': 'GeometryCollection'}
                geom_content = {"geometries": [geom_data]}
                type_geom.update(geom_content)
                geom_data_string = json.dumps(type_geom)
                try:
                    geom_object = GEOSGeometry(geom_data_string)  # GeometryCollection object
                    logging.debug('Found geometry: %s', geom_object)
                    return geom_object
                except:
                    print("Invalid Geometry")
            except ValueError as e:
                print("Not a valid GeoJSON")

def extract_timeperiod_from_html(content):
    period = [None, None]
    for tag in content.find_all("meta"):
        if tag.get("name", None) in ['DC.temporal', 'DC.PeriodOfTime']:
            data = tag.get("content", None)
            period = data.split("/")
            logging.debug('Found time period: %s', period)
            break
    # returning arrays for array field in DB
    return [period[0]], [period[1]]

def parse_oai_xml_and_save_publications(content):
    DOMTree = xml.dom.minidom.parseString(content)
    collection = DOMTree.documentElement
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
        title_value = title[0].firstChild.nodeValue if title else None

        abstract = collection.getElementsByTagName("dc:description")
        abstract_text = abstract[0].firstChild.nodeValue if abstract else None

        journal = collection.getElementsByTagName("dc:publisher")
        journal_value = journal[0].firstChild.nodeValue if journal else None

        date = collection.getElementsByTagName("dc:date")
        date_value = date[0].firstChild.nodeValue if date else None

        publication = Publication(
            title=title_value,
            abstract=abstract_text,
            publicationDate=date_value,
            url=identifier_value,
            journal=journal_value,           # only if your model has 'journal'
            geometry=geom_object,
            timeperiod_startdate=period_start,
            timeperiod_enddate=period_end
        )
        publication.save()
        logger.info('Saved new publication for %s: %s', identifier_value, publication)

def harvest_oai_endpoint(url):
    try:
        with requests.Session() as s:
            response = s.get(url)
            parse_oai_xml_and_save_publications(response.content)
    except requests.exceptions.RequestException as e:
        print("The requested URL is invalid or has bad connection. Please change the URL")


# ---------------------------------------------------------------------
# COMMENTED OUT: Parquet references
# ---------------------------------------------------------------------
# def generate_parquet_file():
#     ...
# def scheduled_generate_parquet():
#     ...
# def schedule_parquet_generation():
#     ...

# ---------------------------------------------------------------------
# NEW CODE: Generate and cache .geojson/.geojson.gz on a schedule
# ---------------------------------------------------------------------

def generate_geojson_files():
    """
    Serialize all Publications to a .geojson file and a .geojson.gz file.
    Store them under 'optimap/fixtures' or another stable location.
    """
    from publications.models import Publication

    queryset = Publication.objects.all()
    geojson_str = serialize(
        'geojson',
        queryset,
        geometry_field='geometry',
        fields=('doi','title','abstract','publicationDate','url')  # adjust as needed
    )

    if not geojson_str:
        logger.warning("No publications found for GeoJSON export.")
        return

    base_dir = os.path.join('optimap', 'fixtures')
    if not os.path.exists(base_dir):
        os.makedirs(base_dir, exist_ok=True)

    geojson_path = os.path.join(base_dir, 'publications.geojson')
    geojson_gz_path = os.path.join(base_dir, 'publications.geojson.gz')

    # Write uncompressed
    with open(geojson_path, 'w', encoding='utf-8') as f:
        f.write(geojson_str)

    # Write gzipped
    with gzip.open(geojson_gz_path, 'wb') as gz:
        gz.write(geojson_str.encode('utf-8'))

    logger.info("Generated %s and %s", geojson_path, geojson_gz_path)


def scheduled_generate_geojson():
    """
    Task function for Django Q to call 'generate_geojson_files'.
    """
    generate_geojson_files()
    logger.info("Scheduled: GeoJSON generation complete.")

def schedule_geojson_generation():
    """
    Create or update a Django Q schedule for daily (or weekly, etc.) generation.
    """
    Schedule.objects.update_or_create(
        func="publications.tasks.scheduled_generate_geojson",
        defaults={
            "schedule_type": Schedule.DAILY,  # D=Daily, H=Hourly, etc.
            "next_run": None,
            "repeats": -1,
        },
    )
    logger.info("Scheduled daily generation of GeoJSON files.")
