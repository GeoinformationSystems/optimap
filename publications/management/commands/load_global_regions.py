import os
import io
import zipfile
import urllib.request
from django.conf import settings
from publications.models import GlobalRegion
from django.core.management.base import BaseCommand
from django.contrib.gis.gdal import DataSource

COMMAND_DIR = os.path.dirname(__file__)
CONTINENTS_URL     = (
    "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
    "World_Continents/FeatureServer/0/query"
    "?where=1%3D1&outFields=*&f=geojson"
) # Esri Hub World Continents Feature Service 
CONTINENTS_FILE    = "world_continents.geojson"

OCEANS_WFS_URL     = (
    "https://geo.vliz.be/geoserver/MarineRegions/wfs?"
    "service=WFS&version=1.0.0&request=GetFeature&"
    "typeName=MarineRegions:iho&outputFormat=application/json"
) # MarineRegions IHO Sea Areas WFS
OCEANS_FILE        = "marine_regions_iho.geojson"

class Command(BaseCommand):
    help = "Load 7 continents (Esri Hub) and 5 oceans (MarineRegions IHO) into GlobalRegion"

    def handle(self, *args, **options):
        self.stdout.write("Downloading Esri World Continents…")
        req = urllib.request.Request(CONTINENTS_URL, headers={"User-Agent":"Mozilla/5.0"})
        continents_path = os.path.join(COMMAND_DIR, CONTINENTS_FILE)
        with urllib.request.urlopen(req) as resp, open(continents_path, "wb") as out:
            out.write(resp.read())

        ds = DataSource(continents_path)
        layer = ds[0]
        for feat in layer:
            name = feat.get("CONTINENT") or feat.get("Name") or feat.get("continent")
            geom = feat.geom.geos

            obj, created = GlobalRegion.objects.update_or_create(
                name=name,
                region_type=GlobalRegion.CONTINENT,
                defaults={
                    "geom":       geom,
                    "source_url": CONTINENTS_URL,
                    "license":    "Esri – World Continents Feature Service",
                }
            )
            verb = "Created" if created else "Updated"
            self.stdout.write(f"{verb} continent '{obj.name}'")

        self.stdout.write("Downloading MarineRegions IHO Sea Areas…")
        req = urllib.request.Request(OCEANS_WFS_URL, headers={"User-Agent":"Mozilla/5.0"})
        oceans_path = os.path.join(COMMAND_DIR, OCEANS_FILE)
        with urllib.request.urlopen(req) as resp, open(oceans_path, "wb") as out:
            out.write(resp.read())

        ds = DataSource(oceans_path)
        layer = ds[0]

        fields = layer.fields
        self.stdout.write(f"MarineRegions IHO fields: {fields}")

        if "name" in fields:
            name_field = "name"
        elif "Name" in fields:
            name_field = "Name"
        else:
            raise RuntimeError(f"No obvious name field; found {fields}")

        for feat in layer:
            name = feat.get(name_field)
            if "Ocean" not in name:
                continue

            geom = feat.geom.geos
            obj, created = GlobalRegion.objects.update_or_create(
                name=           name,
                region_type=    GlobalRegion.OCEAN,
                defaults={
                    "geom":       geom,
                    "source_url": OCEANS_WFS_URL,
                    "license":    "MarineRegions IHO Sea Areas (CC-BY 4.0)",
                }
            )
            verb = "Created" if created else "Updated"
            self.stdout.write(f"{verb} ocean '{obj.name}'")