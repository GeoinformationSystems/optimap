import logging
import time
import os
import socket
from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.exceptions import ValidationError
from django.contrib.gis.geos import Point

from publications.models import Journal
from pyalex import Sources
import requests
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderServiceError

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Sync journal metadata and articles list from OpenAlex"

    def add_arguments(self, parser):
        parser.add_argument(
            '--issn',
            type=str,
            help='If provided, sync only this ISSN-L'
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Use Nominatim for free geocoding; set a custom user-agent
        self.geolocator = Nominatim(user_agent="optimap-sync")

    def handle(self, *args, **options):
        # 0) Unset any proxy environment vars to avoid local interception
        for v in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy'):
            os.environ.pop(v, None)

        verbosity = options.get('verbosity', 1)
        client = Sources()
        qs = Journal.objects.all()
        if options.get('issn'):
            qs = qs.filter(issn_l=options['issn'])

        # 1) Verify DNS resolution
        try:
            resolved = socket.gethostbyname("api.openalex.org")
            self.stdout.write(f"DNS: api.openalex.org → {resolved}")
        except Exception as e:
            self.stdout.write(f"DNS lookup failed: {e}")
            return

        # Bail out if OpenAlex resolves to local/private
        if resolved.startswith(("127.", "10.", "192.168.", "172.16.", "::1")):
            self.stdout.write(
                "api.openalex.org resolves to a local/private IP. "
                "Check your /etc/hosts or corporate DNS."
            )
            return

        # 2) Prepare a session that ignores environment proxies
        session = requests.Session()
        session.trust_env = False  # don’t read HTTP_PROXY/HTTPS_PROXY
        self.stdout.write(f"trust_env={session.trust_env}, proxies={session.proxies}")

        mailto = getattr(settings, "OPENALEX_MAILTO", None)

        for journal in qs:
            if not journal.issn_l:
                if verbosity > 0:
                    self.stdout.write(
                        self.style.WARNING(f"Skipping '{journal.display_name}': no ISSN-L")
                    )
                continue

            # 3) Try PyAlex client first
            fetched = False
            try:
                src = client.get_single_source(journal.issn_l, id_type='issn')
                defaults = {
                    "display_name": src.get('display_name') or journal.display_name,
                    "publisher":    src.get('publisher')    or journal.publisher,
                    "openalex_id":  src.get('id')           or journal.openalex_id,
                    "issn_list":    src.get('issn')         or journal.issn_list,
                }
                # Extract coords if available
                lat = src.get('location', {}).get('lat')
                lon = src.get('location', {}).get('lon')
                if lat is not None and lon is not None:
                    defaults['geometry'] = Point(lon, lat)
                # Fallback: geocode by name if no location at all
                elif not journal.geometry:
                    try:
                        loc = self.geolocator.geocode(defaults['display_name'])
                        if loc:
                            defaults['geometry'] = Point(loc.longitude, loc.latitude)
                    except GeocoderServiceError as ge:
                        logger.debug("Geocoding failed for %s: %s", journal.display_name, ge)

                Journal.objects.update_or_create(
                    issn_l=journal.issn_l,
                    defaults=defaults
                )
                fetched = True
                if verbosity > 1:
                    self.stdout.write(f"SYNCPY ALEX: metadata synced for {journal.issn_l}")
            except Exception as e:
                logger.debug("PyAlex fetch failed for %s: %s", journal.issn_l, e)

            # 4) HTTP fallback if PyAlex didn’t work
            if not fetched:
                url = f"https://api.openalex.org/sources/issn:{journal.issn_l}"
                params = {}
                if mailto:
                    params['mailto'] = mailto

                self.stdout.write(f"Fetching OpenAlex URL: {url}")
                resp = session.get(
                    url,
                    params=params,
                    timeout=10,
                    headers={'Accept': 'application/json'},
                )

                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        logger.error("Expected JSON but got HTML/text for %s", url)
                        continue

                    defaults = {
                        "display_name": data.get('display_name') or journal.display_name,
                        "publisher":    data.get('publisher')    or journal.publisher,
                        "openalex_id":  data.get('id')           or journal.openalex_id,
                        "issn_list":    data.get('issn')         or journal.issn_list,
                    }
                    loc = data.get('location')
                    if loc and loc.get('latitude') and loc.get('longitude'):
                        defaults['geometry'] = Point(loc['longitude'], loc['latitude'])
                    elif not journal.geometry:
                        # geocode by name
                        try:
                            geo = self.geolocator.geocode(defaults['display_name'])
                            if geo:
                                defaults['geometry'] = Point(geo.longitude, geo.latitude)
                        except GeocoderServiceError as ge:
                            logger.debug("Geocoding failed for %s: %s", journal.display_name, ge)

                    Journal.objects.update_or_create(
                        issn_l=journal.issn_l,
                        defaults=defaults
                    )
                    if verbosity > 1:
                        self.stdout.write(f"SYNCHTTP: metadata synced for {journal.issn_l}")
                else:
                    logger.error(
                        "HTTP %d from OpenAlex for %s: %s",
                        resp.status_code, journal.issn_l, resp.text
                    )
                    continue

            # small pause before fetching works
            time.sleep(0.2)

            # 5) Fetch works (articles) for this journal
            try:
                source_id = journal.openalex_id.rstrip('/').rsplit('/', 1)[-1]
                # Use the new filter parameter: locations.source.id
                work_params = {
                    'filter': f'locations.source.id:{source_id}',
                    'per-page': 100
                }
                if mailto:
                    work_params['mailto'] = mailto

                resp_w = session.get(
                    "https://api.openalex.org/works",
                    params=work_params,
                    timeout=10,
                    headers={'Accept': 'application/json'}
                )
                self.stdout.write(f"🔍 Works URL       : {resp_w.url}")
                self.stdout.write(f"🔍 Status code     : {resp_w.status_code}")
                self.stdout.write(f"🔍 Body snippet    : {resp_w.text[:200]!r}")

                if resp_w.status_code == 200:
                    try:
                        works = resp_w.json().get('results', [])
                    except ValueError:
                        logger.error("Expected JSON for works but got HTML/text")
                        continue

                    articles = [w['id'] for w in works if w.get('id')]
                    Journal.objects.update_or_create(
                        issn_l=journal.issn_l,
                        defaults={"articles": articles}
                    )
                    if verbosity > 1:
                        self.stdout.write(
                            f"Fetched {len(articles)} works for {journal.issn_l}"
                        )
                elif resp_w.status_code == 403:
                    logger.error("403 fetching works for %s: %s", journal.issn_l, resp_w.text)
                else:
                    logger.warning(
                        "HTTP %d on works fetch for %s: %s",
                        resp_w.status_code, journal.issn_l, resp_w.text
                    )
            except Exception as e:
                logger.exception("Error fetching works for %s: %s", journal.issn_l, e)

            time.sleep(0.2)
