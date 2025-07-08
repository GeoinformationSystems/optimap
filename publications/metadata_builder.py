import os
import subprocess
from datetime import date

from django.conf import settings
from jinja2 import Environment, FileSystemLoader
from zenodo_client import Creator, Metadata
from zenodo_client.struct import Community

def build_zenodo_metadata() -> Metadata:
    """
    Assemble a Metadata object using our ZenodoService.build_metadata helper.
    """
    # 1. Render a Jinja2 description
    tmpl_dir = os.path.join(settings.BASE_DIR, 'templates')
    env = Environment(loader=FileSystemLoader(tmpl_dir))
    tmpl = env.get_template('zenodo_description.j2')
    description = tmpl.render(
        date=date.today().isoformat(),
        article_count=0,        # replace with actual Article.objects.count()
        journals=[]             # replace with actual journal list
    )

    # 2. Prepare required fields
    creators = [
        Creator(name="Optimap Team", affiliation="Public University")
    ]
    related = [
        {
            "related_identifier": f"https://optimap.science/data/{name}",
            "relation": "IsSupplementTo"
        }
        for name in ["geojson.zip", "geopackage.zip"]
    ]
    communities = [Community(identifier="zenodo")]
    keywords = ["GIS", "FAIR", "optimap"]
    notes = f"Generated {date.today().isoformat()}."

    # 3. Build and return
    from .zenodo_service import ZenodoService
    return ZenodoService.build_metadata(
        title=f"OPTIMAP FAIR data package {date.today().isoformat()}",
        upload_type="dataset",
        description=description,
        creators=creators,
        related_identifiers=related,
        communities=communities,
        keywords=keywords,
        notes=notes,
    )

def collect_upload_paths() -> list[str]:
    """
    Return paths for:
      - data/geojson.zip
      - data/geopackage.zip
      - a git snapshot of the repo
    """
    base = settings.BASE_DIR
    data_dir = os.path.join(base, "data")
    dumps = [os.path.join(data_dir, f) for f in ("geojson.zip", "geopackage.zip")]

    snapshot = os.path.join(data_dir, "optimap-main.zip")
    if not os.path.exists(snapshot):
        subprocess.run(
            ["git", "archive", "--format=zip", "HEAD", "-o", snapshot],
            cwd=base,
            check=True
        )

    return dumps + [snapshot]
