import subprocess
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings
from jinja2 import Environment, FileSystemLoader

from publications.models import Publication, Source


class Command(BaseCommand):
    help = "Generate optimap-main.zip and render README.md into the project's data/ folder"

    def handle(self, *args, **options):
        # 1. Locate project root and ensure data/ exists
        project_root = Path(__file__).resolve().parents[3]
        data_dir = project_root / 'data'
        data_dir.mkdir(exist_ok=True)

        # 2. Archive current Git HEAD to data/optimap-main.zip
        archive_path = data_dir / 'optimap-main.zip'
        self.stdout.write(f"Archiving current branch to {archive_path}")
        subprocess.run(
            ['git', 'archive', '--format=zip', 'HEAD', '-o', str(archive_path)],
            cwd=str(project_root),
            check=True
        )

        # 3. Set up Jinja2 environment for README.md.j2
        tmpl_dir = project_root / 'publications' / 'templates'
        env = Environment(
            loader=FileSystemLoader(str(tmpl_dir)),
            trim_blocks=True,
            lstrip_blocks=True
        )
        template = env.get_template('README.md.j2')

        # 4. Gather stats from your models
        article_count = Publication.objects.count()
        spatial_count = Publication.objects.exclude(geometry=None).count()
        temporal_count = Publication.objects.exclude(timeperiod_startdate=None).count()
        earliest_date = (
            Publication.objects.order_by('publicationDate')
            .values_list('publicationDate', flat=True)
            .first() or ''
        )
        latest_date = (
            Publication.objects.order_by('-publicationDate')
            .values_list('publicationDate', flat=True)
            .first() or ''
        )

        # 5. Build sources list using real model fields
        sources = []
        for collection_name, url_field in Source.objects.values_list(
            'collection_name', 'url_field'
        ):
            url = url_field or f'https://optimap.science/data/{collection_name}.zip'
            sources.append({
                'name': collection_name,
                'url': url,
            })

        # 6. Versioning: bump vN based on last_version.txt
        version_file = data_dir / 'last_version.txt'
        if version_file.exists():
            last = int(version_file.read_text().strip().lstrip('v') or 0)
        else:
            last = 0
        version = f"v{last + 1}"
        version_file.write_text(version)

        # 7. Hard‑coded funding entries
        funders = [
            {
                'name': 'OPTIMETA',
                'project_url': 'https://projects.tib.eu/optimeta',
                'grant_id': '16TOA028B',
                'funder_name': 'Federal Ministry of Education and Research (BMBF)',
                'logo_path': settings.STATIC_URL + 'optimeta-logo.png',
            },
            {
                'name': 'KOMET',
                'project_url': 'https://projects.tib.eu/komet',
                'grant_id': '16KOA009A',
                'funder_name': 'Federal Ministry of Research, Technology and Space (BMFTR)',
                'logo_path': settings.STATIC_URL + 'komet-logo.png',
            },
            {
                'name': 'NFDI4Earth',
                'project_url': 'https://nfdi4earth.de/',
                'grant_id': '460036893',
                'funder_name': 'German Research Foundation (DFG)',
                'logo_path': settings.STATIC_URL + 'nfdi4earth-logo.png',
            },
        ]

        # 8. Render the README.md template
        rendered = template.render(
            version=version,
            date=date.today().isoformat(),
            article_count=article_count,
            sources=sources,
            spatial_count=spatial_count,
            temporal_count=temporal_count,
            earliest_date=earliest_date,
            latest_date=latest_date,
            funders=funders,
        )

        # 9. Write out README.md
        readme_path = data_dir / 'README.md'
        readme_path.write_text(rendered)

        self.stdout.write(self.style.SUCCESS(
            f"Generated assets in {data_dir}:\n"
            f" - {archive_path.name}\n"
            f" - {readme_path.name}"
        ))
