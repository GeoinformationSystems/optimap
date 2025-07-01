# publications/management/commands/sync_source_metadata.py

import logging
import time
import socket
import os
from django.core.management.base import BaseCommand
from django.contrib.gis.geos import Point
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderServiceError
from publications.models import Source
import requests

from pyalex import Sources  # optional, install pyalex for client support

logger = logging.getLogger(__name__)

ISSN_ENDPOINT = "https://api.openalex.org/sources/issn:{issn}"

class Command(BaseCommand):
    help = "Full sync: metadata + geolocation + works list from OpenAlex."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.geolocator = Nominatim(user_agent="optimap-sync")

    def fetch_metadata(self, issn: str) -> dict | None:
        # Try PyAlex first
        try:
            client = Sources()
            return client.get_single_source(issn, id_type="issn")
        except Exception:
            pass

        # Fallback to HTTP
        try:
            resp = requests.get(ISSN_ENDPOINT.format(issn=issn), timeout=10)
            if resp.status_code == 302 and "Location" in resp.headers:
                resp = requests.get(resp.headers["Location"], timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except requests.RequestException as e:
            logger.debug("HTTP metadata fetch failed for %s: %s", issn, e)
        return None

    def handle(self, *args, **options):
        # DNS check
        try:
            ip = socket.gethostbyname("api.openalex.org")
            self.stdout.write(f"DNS: api.openalex.org → {ip}")
        except socket.error as e:
            self.stderr.write(f"DNS lookup failed: {e}")
            return
        if ip.startswith(("127.", "10.", "192.168.", "172.16.", "::1")):
            self.stderr.write("OpenAlex resolves to private IP—aborting.")
            return

        session = requests.Session()
        session.trust_env = False

        for src in Source.objects.exclude(issn_l__isnull=True):
            self.stdout.write(f"Syncing ISSN={src.issn_l}")
            data = self.fetch_metadata(src.issn_l)
            if not data:
                self.stderr.write(f"{src.issn_l}: no metadata\n")
                continue

            defaults = {
                "openalex_id":    data.get("id"),
                "openalex_url":   data.get("id"),
                "publisher_name": (data.get("host_organization") or {}).get("display_name")
                                   or data.get("display_name"),
            }

            # geolocation from OpenAlex
            loc = data.get("location", {})
            lat, lon = loc.get("lat"), loc.get("lon")
            if lat and lon:
                defaults["geometry"] = Point(lon, lat)
            elif not src.geometry:
                # fallback geocode by name
                try:
                    geo = self.geolocator.geocode(defaults["publisher_name"])
                    if geo:
                        defaults["geometry"] = Point(geo.longitude, geo.latitude)
                except GeocoderServiceError as ge:
                    logger.debug("Geocoding failed: %s", ge)

            # save metadata & geometry
            src, _ = Source.objects.update_or_create(issn_l=src.issn_l, defaults=defaults)
            self.stdout.write(f"{src.issn_l}: metadata & geo synced")

            # fetch works list
            source_id = src.openalex_id.rstrip("/").split("/")[-1]
            resp = session.get(
                "https://api.openalex.org/works",
                params={"filter": f"locations.source.id:{source_id}", "per-page": 100},
                timeout=30,
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                ids = [w["id"] for w in results if w.get("id")]
                src.articles = ids
                src.save(update_fields=["articles"])
                self.stdout.write(f"{src.issn_l}: fetched {len(ids)} works")
            else:
                logger.warning("Works fetch %s → %s", resp.status_code, resp.text)

            time.sleep(0.2)

        self.stdout.write("Full sync complete.")
