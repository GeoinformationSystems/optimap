"""Management command wrapper for render_zenodo_package()."""
from django.core.management.base import BaseCommand

from works.zenodo import render_zenodo_package


class Command(BaseCommand):
    help = "Generate optimap-main.zip, data/README.md and data/zenodo_dynamic.json."

    def handle(self, *args, **options):
        result = render_zenodo_package(stdout_callback=self.stdout.write)

        self.stdout.write(self.style.SUCCESS(
            f"Generated assets in {result['data_dir']}:\n"
            f" - {result['archive_path'].name}\n"
            f" - {result['readme_path'].name}\n"
            f" - {result['metadata_path'].name}"
        ))
