import os
import requests
from datetime import datetime
from django.conf import settings

from wikibaseintegrator.wbi_exceptions import ModificationFailed
from wikibaseintegrator import WikibaseIntegrator
from wikibaseintegrator.wbi_login import Login
from wikibaseintegrator.datatypes import (
    MonolingualText,
    Time,
    String,
    ExternalID,
    GlobeCoordinate
)
try:
    from wikibaseintegrator.datatypes import Url
except ImportError:
    from wikibaseintegrator.datatypes import URL as Url

# Our instance’s SPARQL endpoint (for local lookups by DOI)
if "www.wikidata.org/w/api.php" in settings.WIKIBASE_API_URL:   
    SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
else:
    SPARQL_ENDPOINT = settings.WIKIBASE_API_URL.replace("/w/api.php", "/query/sparql")

# constant for all dates
CALENDAR_MODEL = "http://www.wikidata.org/entity/Q1985727"

# Wikidata property IDs mapping
P_TITLE             = "P1476"  # title (monolingual text)
P_ABSTRACT          = "P1810"  # abstract
P_URL               = "P856"   # official website / URL
P_PUBLICATION_DATE  = "P577"   # publication date
P_PERIOD_START      = "P580"   # start time
P_PERIOD_END        = "P582"   # end time
P_DOI               = "P356"   # DOI as External ID
P_AUTHOR_STRING     = "P2093"  # author name string
P_JOURNAL_NAME      = "P1448"  # journal name (monolingual text)
P_GEOMETRY          = "P625"   # coordinate location

def normalize_date_and_precision(date_str):
    parts = date_str.split("-")
    if len(parts) == 1 and parts[0].isdigit():
        # "YYYY"
        return f"{parts[0]}-01-01", 9
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        # "YYYY-MM"
        return f"{parts[0]}-{parts[1]}-01", 10
    # assume full "YYYY-MM-DD"
    return date_str, 11

def add_time_claims(dates, prop_nr, statements):
    for ds in dates:
        iso, prec = normalize_date_and_precision(ds)
        timestamp = f"+{iso}T00:00:00Z"
        statements.append(Time(
            prop_nr=prop_nr,
            time=timestamp,
            timezone=0,
            before=0,
            after=0,
            precision=prec,
            calendarmodel=CALENDAR_MODEL
        ))


def find_local_item_by_doi(doi):
    """
    Return the Q-ID of an existing item in our Wikibase instance for the given DOI,
    or None if no match is found.
    """
    sparql_query = f'''
    SELECT ?item WHERE {{
      ?item wdt:{P_DOI} "{doi}" .
    }} LIMIT 1
    '''
    response = requests.get(
        SPARQL_ENDPOINT,
        params={"query": sparql_query, "format": "json"},
        headers={"Accept": "application/json"}
    )
    response.raise_for_status()

    data = response.json()
    bindings = data.get("results", {}).get("bindings", [])
    if not bindings:
        return None

    item_uri = bindings[0]["item"]["value"]
    return item_uri.rsplit("/", 1)[-1]

def upsert_publication(publication, wikibase_integrator):
    """
    Create or update a single Publication on Wikibase.
    Returns a tuple (action, qid):
      - action is "created", "updated", or "skipped"
      - qid is the Wikibase item ID (or None if skipped)
    """
    # 1) Build statements
    iso_date = publication.publicationDate.isoformat()
    publication_timestamp = f"+{iso_date}T00:00:00Z"

    statements = [
        MonolingualText(prop_nr=P_TITLE, text=publication.title, language="en"), 
        Time(prop_nr=P_PUBLICATION_DATE, time=publication_timestamp, timezone=0, before=0, after=0, precision=11, calendarmodel=CALENDAR_MODEL), 
        String(prop_nr=P_AUTHOR_STRING, value=(publication.created_by.username if publication.created_by else "Unknown author")),
    ]

    if publication.abstract:
        statements.append(String(prop_nr=P_ABSTRACT, value=publication.abstract))

    if publication.url:
        statements.append(Url(prop_nr=P_URL, value=publication.url))

    if publication.timeperiod_startdate:
        add_time_claims(publication.timeperiod_startdate, P_PERIOD_START, statements)

    if publication.timeperiod_enddate:
        add_time_claims(publication.timeperiod_enddate,   P_PERIOD_END, statements)

    if publication.source:
        statements.append(MonolingualText(prop_nr=P_JOURNAL_NAME, text=publication.source, language="en"))

    if publication.doi:
        statements.append( ExternalID(prop_nr=P_DOI, value=publication.doi))

    if publication.geometry:
        geometries = getattr(publication.geometry, "geoms", [publication.geometry])
        for geom in geometries:
            if getattr(geom, "geom_type", None) != "Point":
                geom = geom.centroid
            statements.append(GlobeCoordinate(prop_nr=P_GEOMETRY, latitude=geom.y, longitude=geom.x, precision=0.0001))

    # 7) Check for existing item by DOI
    existing_qid = find_local_item_by_doi(publication.doi) if publication.doi else None

    if existing_qid:
        # Update existing item
        entity = wikibase_integrator.item.get(entity_id=existing_qid)
        entity.claims.add(statements)
        try:
            entity.write(summary="Update publication via OptimapBot")
            return "updated", existing_qid
        except ModificationFailed as e:
            if "already has label" in str(e):
                return "skipped", existing_qid
            raise
    else:
        # Create new item
        entity = wikibase_integrator.item.new()
        entity.labels.set("en", publication.title)
        entity.descriptions.set("en", "Publication imported from Optimap")
        entity.claims.add(statements)
        try:
            write_result = entity.write(summary="Create publication via OptimapBot")
            created_qid = write_result.get("entity", {}).get("id")
            return "created", created_qid
        except ModificationFailed as e:
            if "already has label" in str(e):
                return "skipped", None
            raise

def export_publications_to_wikidata(publications):
    login_session = Login(
        user=settings.WIKIBASE_USERNAME,
        password=settings.WIKIBASE_PASSWORD,
        mediawiki_api_url=settings.WIKIBASE_API_URL,
    )
    wikibase_client = WikibaseIntegrator(login=login_session)

    created_count = 0
    updated_count = 0
    error_records = []

    for publication in publications:
        if not publication.publicationDate:
            error_records.append((publication, "no publicationDate"))
            continue

        try:
            action, entity_id = upsert_publication(publication, wikibase_client)
            if action == "created":
                created_count += 1
            elif action == "updated":
                updated_count += 1
        except Exception as err:
            error_records.append((publication, str(err)))

    return created_count, updated_count, error_records
