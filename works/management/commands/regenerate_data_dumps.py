# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regenerate the public data-dump files (GeoJSON + GeoPackage + CSV).

By default this runs the umbrella ``works.tasks.regenerate_all_data_dumps``,
producing all three formats from a single PostGIS pass and pruning older
cycles per ``OPTIMAP_DATA_DUMP_RETENTION``. The same operation is available
via the Django-Q schedule (every ``DATA_DUMP_INTERVAL_HOURS`` hours, default
6) and the admin "Regenerate all data exports now" action; this CLI gives
operators a third path that runs synchronously in the current process —
useful when the Q cluster isn't running, when scripting a deploy, or when
debugging a regen failure interactively.

Usage:
    python manage.py regenerate_data_dumps
    python manage.py regenerate_data_dumps --format csv      # only CSV
    python manage.py regenerate_data_dumps --format gpkg     # only GeoPackage
    python manage.py regenerate_data_dumps --format geojson  # only GeoJSON
    python manage.py regenerate_data_dumps --dry-run         # say what would run
"""

from django.core.management.base import BaseCommand, CommandError

from works.tasks import (
    human_size,
    regenerate_all_data_dumps,
    regenerate_csv_cache,
    regenerate_geojson_cache,
    regenerate_geopackage_cache,
)

_FORMAT_DISPATCH = {
    "geojson": regenerate_geojson_cache,
    "gpkg": regenerate_geopackage_cache,
    "csv": regenerate_csv_cache,
}


class Command(BaseCommand):
    help = "Regenerate the public data-dump files (GeoJSON + GeoPackage + CSV)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--format",
            choices=sorted(_FORMAT_DISPATCH),
            help=(
                "Regenerate only this format. Default runs the umbrella that "
                "shares one GeoJSON intermediate across all three."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be regenerated without writing files.",
        )

    def handle(self, *args, **opts):
        fmt = opts["format"]
        dry_run = opts["dry_run"]

        if dry_run:
            target = fmt or "all (geojson + gpkg + csv)"
            self.stdout.write(f"Would regenerate: {target}")
            return

        if fmt is None:
            results = regenerate_all_data_dumps()
            for name, path in results.items():
                if path is None:
                    self.stdout.write(self.style.WARNING(f"  {name}: FAILED (see logs)"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"  {name}: {path} ({human_size(path)})"))
            ok = sum(1 for p in results.values() if p is not None)
            self.stdout.write(self.style.SUCCESS(f"Done — regenerated {ok}/{len(results)} format(s)."))
            return

        path = _FORMAT_DISPATCH[fmt]()
        if path is None:
            raise CommandError(f"{fmt} regeneration failed (see logs).")
        self.stdout.write(self.style.SUCCESS(f"Regenerated {fmt}: {path} ({human_size(path)})"))
