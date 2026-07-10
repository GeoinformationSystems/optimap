# OPTIMAP

[![Project Status: WIP – Initial development is in progress, but there has not yet been a stable, usable release suitable for the public.](https://www.repostatus.org/badges/latest/wip.svg)](https://www.repostatus.org/#wip) [![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.8198944.svg)](https://doi.org/10.5281/zenodo.8198944)

Geospatial discovery of research articles based on open metadata.
The OPTIMETA Portal is part of the OPTIMETA project (<https://projects.tib.eu/optimeta>) and relies on the spatial and temporal metadata collected for scientific papers with the OPTIMETA Geo Plugin for Open Journal Systems ([OJS](https://pkp.sfu.ca/ojs/)) published at <https://github.com/TIBHannover/optimetaGeo>.
The product name of the portal is OPTIMAP.
The development is continued in the project KOMET (<https://projects.tib.eu/komet>).

The OPTIMAP has the following features:

- Start page with a full screen map (showing geometries and metadata) and a time line of the areas and time periods of interest for scientific works
- Passwordless login via email
- RESTful API at `/api`
- **Multi-source harvesting**: OAI-PMH, RSS, Crossref-prefix, Janeway, and Mountain Wetlands Repository — each with per-source deduplication, careful re-harvest updates that preserve user-contributed metadata, and OpenAlex enrichment to fill gaps
- **Curated collections**: groupings of works under a curator-managed identifier (e.g. `mountain-wetlands`), with public `/collections/<identifier>/` pages, vanity short slugs, and per-collection curator roles to add/remove works from any landing page
- **Crowdsourced metadata contribution**: Logged-in users can contribute spatial and temporal extent data for works
- **Publication workflow**: Harvested → Contributed → Published status transitions with full provenance tracking
- **Admin controls**: Publish/unpublish functionality with audit trails
- **Recognition Board** at `/recognition-board/`: opt-in public leaderboard for contributors of spatial/temporal metadata, organised into explorer-named tiers
- **Subscriptions**: email notifications for new works matching a region (continent / ocean / custom bbox)
- **Regional feeds and data downloads**: per-region GeoRSS / GeoAtom feeds and full-corpus GeoJSON / GeoPackage / CSV (with WKT geometry column) exports refreshed every six hours
- **Reference-manager / Zotero compatibility**: landing and collection pages emit Highwire Press `citation_*` tags, schema.org `ScholarlyArticle` JSON-LD, and a COinS fallback so the Zotero browser connector and similar tools save complete bibliographic records (with PDF when known)
- **Sharing-friendly metadata**: Open Graph, Twitter Card, schema.org, and Google Scholar tags on landing pages, plus a per-work `og:image` map preview
- **Geoextent service** at `/geoextent/`: interactive tool to extract spatial/temporal extents from uploaded files or remote repositories (Zenodo, PANGAEA, OSF, Figshare, Dryad, Dataverse, GFZ Data Services)

## Work Status Workflow

Works in OPTIMAP follow a status-based workflow with six possible states:

### Status Definitions

- **Draft** (`d`): Internal draft state. Not visible to public. Can be edited by admins. Created when unpublishing a published work.
- **Harvested** (`h`): Automatically harvested from OAI-PMH or RSS feeds. May or may not have spatial/temporal extent. Not publicly visible.
- **Contributed** (`c`): User has contributed spatial and/or temporal extent. Awaits admin review. Not publicly visible.
- **Published** (`p`): Public-facing works visible to all users via website, map, API, and feeds.
- **Testing** (`t`): Reserved for testing purposes. Not publicly visible. Admin access only.
- **Withdrawn** (`w`): Publication has been withdrawn or retracted. Not publicly visible.

### Workflow Transitions

**Harvesting → Publishing:**

1. Publication harvested from external source → Status: **Harvested** (`h`)
2. User contributes spatial/temporal extent → Status: **Contributed** (`c`)
3. Admin reviews and approves → Status: **Published** (`p`)
4. If needed, admin can unpublish → Status: **Draft** (`d`)

**Direct Publishing (Skip Contribution):**

- Harvested works with **at least one extent type** (spatial OR temporal) can be published directly by admins without user contribution

**Contribution Requirements:**

- Users can only contribute to works with **Harvested** (`h`) status
- Harvested works **without any extent** require user contribution before publishing
- Contributed works can be published after admin review

**Visibility Rules:**

- Only **Published** (`p`) status is visible to non-admin users
- All other statuses require admin privileges to view
- Published works appear in: main map, work list, API responses, RSS/Atom feeds

**Extent Contribution:**

- Users can contribute **spatial extent** (geographic location) via interactive map with drawing tools
- Users can contribute **temporal extent** (time period) via date form (formats: YYYY, YYYY-MM, YYYY-MM-DD)
- Both extent types can be contributed separately or together in a single submission
- Works without DOI are supported via ID-based URLs (`/work/<id>/`)
- All contributions are tracked with full provenance (user, timestamp, changes)
- Contribute page lists works missing either spatial OR temporal extent

OPTIMAP is based on [Django](https://www.djangoproject.com/) (with [GeoDjango](https://docs.djangoproject.com/en/4.1/ref/contrib/gis/) and [Django REST framework](https://www.django-rest-framework.org/)) with a [PostgreSQL](https://www.postgresql.org/)/[PostGIS](https://postgis.net/) database backend.

This README covers setup, development, and deployment. For day-to-day operation of a running instance — managing harvesting sources, curating collections, blocking users, running the Django-Q cluster — see the operator handbook at [docs/manage.md](docs/manage.md).

The development of OPTIMAP was and is supported by the projects [OPTIMETA](https://projects.tib.eu/optimeta/en/) and [KOMET](https://projects.tib.eu/komet/en/).

[![OPTIMETA Logo](https://projects.tib.eu/fileadmin/_processed_/e/8/csm_Optimeta_Logo_web_98c26141b1.png)](https://projects.tib.eu/optimeta/en/) [![KOMET Logo](https://projects.tib.eu/fileadmin/templates/komet/tib_projects_komet_1150.png)](https://projects.tib.eu/komet/en/)

## Configuration

All configuration is done via the file `optimap/settings.py`.
Configurations that need to be changed for different installations and for deployment are also exposed as environment variables.
The names of these environment variables start with `OPTIMAP_`.
The settings files loads these from a file `.env` stored in the same location as `settings.py`, or from the environment the server is run it.
A complete list of existing parameters is provided in the file `optimap/.env.example`.

## Run with Docker

OPTIMAP is containerised. To bring up the full stack (app, PostGIS, Nginx) — for local use or production — see [docs/deploy-docker-compose.md](docs/deploy-docker-compose.md).

## Development

### Test Data

The folder `/fixtures` contains some test data, which can be used to populate the database during development. These data are provided either as SQL commands for insertion into the database or as database dumps that can be loaded using [`django-admin`](https://docs.djangoproject.com/en/dev/ref/django-admin/).

### Managing Test Data

#### Creating Test Data Dumps

To create a data dump after generating or harvesting test data, use the following command:

```bash
python manage.py dumpdata --exclude=auth --exclude=contenttypes | jq > fixtures/test_data.json
```

#### Loading Test Data

To load the test data into your database, run the following command choosing one of the existing fixtures:

```bash
python manage.py loaddata fixtures/test_data_{optimap, partners, global_feeds}.json
```

`fixtures/test_data_global_feeds.json` backs [tests/test_feeds_global_landing_pages.py](tests/test_feeds_global_landing_pages.py)
and exercises the spatial queries behind the global/regional GeoRSS and Atom feeds. It intentionally
covers a wide range of geometry shapes — single points, linestrings, single polygons, `MULTIPOLYGON`
geometries, `GEOMETRYCOLLECTION`s containing multiple separate polygons, and works whose geometry spans
multiple continents, multiple oceans, or one ocean and one continent — so that `geometry__intersects`
filtering is checked against each case, not just simple single-polygon works. If you add or modify
entries in this fixture, re-run `python manage.py test tests.test_feeds_global_landing_pages` and update
the `EXPECTED_COUNTS` dict in that test file to match the new per-region work counts.

#### Adding New Test Data

If additional test data is required, you can:

- Copy and paste existing records.
- Add new records via the Django admin interface (preferred method).
- Manually modify the test data JSON file.

Tools for Geometries
For creating or editing geometries, consider using the tool [WKTMap](https://wktmap.com/), which provides an easy way to generate Well-Known Text (WKT) representations of spatial data.

### Run Locally

1. Create a `.env` file based on `.env.example` in the same directory where `settings.py` resides, and fill in the configuration settings as needed.
2. Run the commands below to set up and run the project locally. This setup uses the built-in Python [`venv`](https://docs.python.org/3/library/venv.html). Refer to [this tutorial](https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/#create-and-use-virtual-environments) for more details.

```bash
# Create a virtual environment (once only)
python -m venv .venv

# Activate the virtual environment
source .venv/bin/activate

# Confirm Python path
which python

# Install GDAL
gdalinfo --version

# Install gdal Pyhton library matching your GDAL version
pip install gdal=="$(gdal-config --version).*"

# Install Python dependencies
pip install -r requirements.txt

# create local DB container (once)
# docker run --name optimapDB -p 5432:5432 -e POSTGRES_USER=optimap -e POSTGRES_PASSWORD=optimap -e POSTGRES_DB=optimap -d postgis/postgis:14-3.3
# get a clean one later: docker rm -f optimapDB

# Start the database container
docker start optimapDB

# Apply database migrations
python manage.py makemigrations
python manage.py migrate

# Create a cache table
python manage.py createcachetable

# Collect static files
python manage.py collectstatic --noinput

# If you need to run tasks (harvesting, data export) then start a cluster in a separate shell
python manage.py qcluster

# If you want to use the predefined feeds for continents and oceans we need to load the geometries for global regions
# On first run this auto-downloads the source data (Esri World Continents and MarineRegions
# Global Oceans and Seas v1, ~128 MB GPKG) and simplifies the ocean polygons (~4.7 MB GeoJSON)
# before loading them into the GlobalRegion table. Tune via OPTIMAP_OCEAN_SIMPLIFICATION_TOLERANCE
# / OPTIMAP_OCEAN_SIMPLIFICATION_PERCENTILE; cache location via OPTIMAP_GLOBAL_REGIONS_DATA_DIR.
# Delete the cached files in that directory to force a fresh download / re-simplification.
python manage.py load_global_regions

# Generate the OGC API - Features OpenAPI document (required once to activate /ogcapi/)
# Re-run with --force whenever etc/pygeoapi-config.yml changes.
# Connects to the same database as Django (derived from DATABASE_URL).
python manage.py generate_pygeoapi_openapi

# Harvest works from real sources
python manage.py harvest_sources --list  # List available sources
python manage.py harvest_sources --all --max-records 20 --create-sources  # Initial harvesting of all sources (limited to 20 records each)
python manage.py harvest_sources --source essd --source geo-leo  # Harvest specific sources

# Start the Django development server
python manage.py runserver

# Start the app with specific configurations for development
OPTIMAP_CACHE=dummy OPTIMAP_DEBUG=True python manage.py runserver

# Start the app with specific configurations for development at a different port
OPTIMAP_CACHE=dummy OPTIMAP_DEBUG=True python manage.py runserver 8002

# Manually regenerating data export files (GeoJSON / GeoPackage / CSV)
## Synchronous in-process (no Q cluster needed): regenerates all three formats from a single PostGIS pass.
python manage.py regenerate_data_dumps
## Restrict to a single format if needed (geojson | gpkg | csv):
python manage.py regenerate_data_dumps --format csv
## Via Django-Q cluster: enqueue the umbrella task on a running cluster (e.g. `python manage.py qcluster`):
python manage.py shell -c "from django_q.tasks import async_task; async_task('works.tasks.regenerate_all_data_dumps')"
```

Now open a browser at <http://127.0.0.1:8000/>.

#### Additional Setup

- Creating a Superuser: Refer to the instructions below to create a superuser for accessing the admin interface.
- Loading Test Data: Refer to the instructions above to load test data into the database.

#### Shutting Down

When finished, deactivate the virtual environment and stop the database container:

```bash
deactivate

docker stop optimapDB
```

#### Debug Mode Configuration

By default, `OPTIMAP_DEBUG` is now set to `False` to ensure a secure and stable production environment. If you need to enable debug mode for development purposes, explicitly set the environment variable in your `.env` file or pass it as an argument when running the server.

#### Enable Debug Mode for Development

To enable debug mode, add the following to your `.env` file:

```env
OPTIMAP_DEBUG=True
```

### Debug with VS Code

Select the Python interpreter created above (`optimap` environment), see instructions at <https://code.visualstudio.com/docs/python/tutorial-django> and <https://code.visualstudio.com/docs/python/environments>.

**Note:** The `docker-compose.yml` file is intended for **development**, not deployment.

Configuration for debugging with VS Code:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Python: Django Run",
      "type": "python",
      "request": "launch",
      "program": "${workspaceFolder}/manage.py",
      "args": ["runserver"],
      "env": {
        "OPTIMAP_DEBUG": "True",
        "OPTIMAP_CACHE": "dummy"
      },
      "django": true,
      "justMyCode": true
    }
  ]
}
```

### Debug email sending

Add `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` to the `.env` file to have emails printed to the console instead of sent via SMTP.

Alternatively, you can run a local STMP server with the following command and configuration:

```bash
python -m smtpd -c DebuggingServer -n localhost:5587
```

```env
OPTIMAP_EMAIL_HOST=localhost
OPTIMAP_EMAIL_PORT=5587
```

### Accessing list of works

Visit the URL - <http://127.0.0.1:8000/works/>

### Harvest publications from real sources

> Triggering harvests from the Django admin (rather than the CLI) and inspecting harvesting events / logs is documented in [docs/manage.md → Manage harvesting](docs/manage.md#manage-harvesting).

The `harvest_sources` management command allows you to harvest publications from real sources (OAI-PMH, RSS/Atom, Crossref, OpenAlex) directly into your database. This is useful for:

- Populating your database with real data for testing and development
- Testing harvesting functionality against live endpoints
- Initial data loading for production deployment

**List available sources**:

```bash
python manage.py harvest_sources --list
```

**Harvest all configured sources** (with record limit):

```bash
python manage.py harvest_sources --all --max-records 50
```

**Harvest specific sources**:

```bash
# Single source
python manage.py harvest_sources --source essd --max-records 100

# Multiple sources
python manage.py harvest_sources --source essd --source geo-leo --source agile-giss
```

**Create source entries automatically**:

```bash
python manage.py harvest_sources --source essd --create-sources
```

**Bulk-insert all configured sources as Source rows (no harvesting)**:

```bash
# Insert every enabled source from SOURCE_CONFIG so it appears in /admin/works/source/
python manage.py harvest_sources --insert-sources

# Also insert sources whose upstream is currently disabled
python manage.py harvest_sources --insert-sources --include-disabled
```

This is the fastest way to bootstrap a fresh deployment so the source list shows up in the Django admin and can be triggered from there. Existing rows (matched by name or URL) are left untouched; the command is idempotent. RSS and Crossref-prefix entries are inserted as plain `Source` rows; the auto-schedule and the admin "Trigger harvesting" action call `works.tasks.harvest_oai_endpoint`, so those non-OAI sources still need the CLI route (`--source <key>`) to harvest until the dispatch logic is generalised — the command prints a warning naming each affected source.

**Associate with specific user**:

```bash
python manage.py harvest_sources --all --user-email admin@optimap.science
```

The command supports OAI-PMH, Crossref-prefix, GeoScienceWorld, MaRESS API, and OpenAlex sources,
automatically dispatching to the correct harvester for each source.

**Full source list**: see [docs/sources.md](docs/sources.md) for all configured sources grouped by publisher,
with keys, names, harvest types, and ISSN/OpenAlex identifiers.

### Create Superusers/Admin

Superusers or administrators can be created using the `createsuperuser` command. This user will have access to the Django admin interface.

```bash
python manage.py createsuperuser --username=optimap --email=nomail@optimap.science
```

You will be prompted for a password. After entering one, the superuser will be created immediately. If you omit the --username or --email options, the command will prompt you for those values interactively.

Access the admin interface at <http://127.0.0.1:8000/admin/>. Once signed in, see [docs/manage.md](docs/manage.md) for the operator workflows the admin exposes (harvesting, collections, blocked emails, the Django-Q cluster).

#### Running in a Dockerized App

To create a superuser within the containerized application, use the following command:

```bash
docker-compose run web python manage.py createsuperuser
```

This will run the same process as above but within the Docker environment. Ensure the container is running and accessible before executing this command

### Run tests

See <https://docs.djangoproject.com/en/4.1/topics/testing/overview/> for testing Django apps.

UI tests are based on [Helium](https://github.com/mherrmann/selenium-python-helium) (because [Pylenium](https://github.com/ElSnoMan/pyleniumio) would need pytest in addition).

```bash
pip install -r requirements-dev.txt
```

#### Unit Tests

Run all unit tests:

```bash
python manage.py test tests

# show deprecation warnings
python -Wa manage.py test

# configure logging level for cleaner test progress output
OPTIMAP_LOGGING_LEVEL=WARNING python manage.py test tests
```

#### Integration Tests (Real Harvesting)

Integration tests that harvest from live OAI-PMH endpoints are disabled by default to avoid network dependencies and slow test execution. These tests verify harvesting from real journal sources.

Run all integration tests:

```bash
# Enable real harvesting tests
SKIP_REAL_HARVESTING=0 python manage.py test tests.test_real_harvesting
```

Run a specific journal test:

```bash
# Test ESSD harvesting
SKIP_REAL_HARVESTING=0 python manage.py test tests.test_real_harvesting.RealHarvestingTest.test_harvest_essd

# Test GEO-LEO harvesting
SKIP_REAL_HARVESTING=0 python manage.py test tests.test_real_harvesting.RealHarvestingTest.test_harvest_geo_leo
```

Show skipped tests (these are skipped by default):

```bash
# Run with verbose output to see skip reasons
python manage.py test tests.test_real_harvesting -v 2
```

**Supported journals**:

- Earth System Science Data (ESSD) - [Issue #59](https://github.com/GeoinformationSystems/optimap/issues/59)
- AGILE-GISS - [Issue #60](https://github.com/GeoinformationSystems/optimap/issues/60)
- GEO-LEO e-docs - [Issue #13](https://github.com/GeoinformationSystems/optimap/issues/13)
- ESS Open Archive (EssOAr) - [Issue #99](https://github.com/GeoinformationSystems/optimap/issues/99) _(endpoint needs confirmation)_

### Run UI tests

Uses Django's `StaticLiveServerTestCase` to start a live server for testing and full control over the test database in each test class.

```bash
python -Wa manage.py test tests-ui
```

### Check test coverage

```bash
# run the tests and capture coverage
coverage run --source='publications' --omit='*/migrations/**' manage.py test tests

# show coverage report
coverage report --show-missing --fail-under=70

# save the reports
coverage html
coverage xml
```

### Develop tests

For developing the UI tests, you can remove the `headless=True` in the statements for starting the browsers so you can "watch along" and inspect the HTML when a breakpoint is hit as the tests are executed.

### Debug tests with VS Code

A configuration to debug the test code and also print deprecation warnings:

```json
{
  "name": "Python: Django Test",
  "type": "python",
  "request": "launch",
  "pythonArgs": ["-Wa"],
  "program": "${workspaceFolder}/manage.py",
  "args": ["test", "tests"],
  "env": {
    "OPTIMAP_DEBUG": "True"
  },
  "django": true,
  "justMyCode": true
}
```

Change the argument `tests` to `tests-ui` to run the UI tests.

See also documentation at <https://code.visualstudio.com/docs/python/tutorial-django>.

<!-- REUSE-IgnoreStart -->

### SEO metadata

Work landing pages, the homepage, and the regional feed pages emit Open Graph, Twitter Card, and schema.org JSON-LD via [django-meta](https://github.com/nephila/django-meta). The OPTIMAP-side helpers live in [`works/seo.py`](works/seo.py); each view that wants metadata builds a `Meta` object and passes it as `context['meta']`. The base template (`works/templates/base.html`) renders `meta/meta.html` from django-meta when that key is present.

Per-page extras:

- **Work landing**: schema.org `ScholarlyArticle` (with `spatialCoverage`/`temporalCoverage` mirroring what we consume from Janeway), Google Scholar `citation_*` tags, and an Open Graph image at `/work/<identifier>/preview.png`.
- **Homepage**: schema.org `WebSite` + `SearchAction`.
- **Regional feed pages**: schema.org `CollectionPage` with the region as `about` (a `Place` with the bbox).

The preview image is generated by [`works/services/preview_image.py`](works/services/preview_image.py) using `staticmap`, cached on disk under `<tmpdir>/optimap_cache/work_previews/`, and invalidated by the `post_save` signal on `Work`. Works without geometry omit `og:image` entirely.

To preview the metadata locally, browse to a published work and inspect `<head>` — the JSON-LD blob, all `og:*`, `twitter:*`, and `citation_*` tags should be present.

Configure deployment-side hostnames via the `OPTIMAP_META_SITE_PROTOCOL` and `OPTIMAP_META_SITE_DOMAIN` environment variables (defaults are dev-friendly: `http` and `localhost:8000`).

### License headers (REUSE)

OPTIMAP follows the [REUSE 3.3 specification](https://reuse.software/spec/) for licensing.
Every first-party source file carries a two-line SPDX header; vendored, generated, and binary files are blanket-licensed via [`REUSE.toml`](REUSE.toml).

**Header template** (use the file's first-commit year for the copyright year):

```text
SPDX-FileCopyrightText: <year> OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
SPDX-License-Identifier: GPL-3.0-or-later
```

Adapted to the file's comment style:

| Extension | Comment style |
| --- | --- |
| `.py`, `.sh`, `.toml`, `.cfg` | `# …` |
| `.js` | `// …` |
| `.css` | `/* … */` block |
| `.html` (Django template) | `{# … #}` |

**Workflow when adding a new file:**

1. Add the two SPDX lines at the top (after a shebang or `<!DOCTYPE>` if present).
2. Run `reuse lint` to confirm the project is still compliant.

**Useful commands** (the tool ships in [`requirements-dev.txt`](requirements-dev.txt)):

```bash
# Verify all files carry copyright + license info (must exit 0 on main)
reuse lint

# Inspect the license/copyright tree as REUSE sees it
reuse spdx | less

# Add a header to a single file (interactive)
reuse annotate \
    --copyright "OPTIMETA and KOMET projects <https://projects.tib.eu/komet>" \
    --license   GPL-3.0-or-later \
    --year      "$(git log --diff-filter=A --follow --format=%ad --date=format:%Y -- <path> | tail -1)" \
    <path>

# Fetch any new SPDX license text into LICENSES/
reuse download --all
```

If a file should not carry an inline header (auto-generated, binary, or
vendored), add it to a matching path pattern in [`REUSE.toml`](REUSE.toml)
rather than committing an unheadered file.

<!-- REUSE-IgnoreEnd -->

### Dependency license checks

OPTIMAP is GPL-3.0-or-later. A CI job (`licenses` in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs
[`pip-licenses`](https://github.com/raimon49/pip-licenses) against every
installed package (including transitive dependencies) and fails if any
package has no detectable license metadata (`--fail-on="Unknown"`). This is
separate from [License headers (REUSE)](#license-headers-reuse), which covers
first-party file headers, not third-party dependency licenses.

Known exceptions — packages whose PyPI metadata omits the license field but
whose actual license has been manually verified — are listed in
[`pyproject.toml`](pyproject.toml) under `[tool.license-check]`, each with a
comment documenting the real license and where it was confirmed.

**Workflow when CI fails on a new "Unknown" license:**

1. Identify the package and version from the CI log (`pip-licenses --order=license --format=plain-vertical` output, also uploaded as the `license-report` artifact).
2. Look up its actual license on its GitHub repo or PyPI project page.
3. If the license is compatible with GPL-3.0-or-later (permissive: MIT/BSD/Apache; weak copyleft: LGPL/MPL; or GPL itself), add the package to `pyproject.toml`:

   ```toml
   [tool.license-check]
   ignore-packages = [
       "existing-package",  # ...
       "new-package",  # <license> — <source URL>
   ]
   ```

4. If the license is **not** compatible (e.g. proprietary, AGPL when not acceptable, or a license incompatible with GPL-3.0), do not add an exception: replace the dependency or consult the maintainers.
5. Push the change; the `licenses` CI job will pass once the package is either ignored (step 3) or removed.

**Useful commands** (the tool ships in [`requirements-dev.txt`](requirements-dev.txt)):

```bash
# Full license report for all installed packages, grouped by license
pip-licenses --order=license --format=plain-vertical

# Same check CI runs (fails with exit 1 if any package's license is undetected)
pip-licenses --fail-on="Unknown" --ignore-packages lazrs staticmap

# Machine-readable report (also produced as a CI artifact)
pip-licenses --order=license --format=json --output-file=licenses.json
```

### Issues during development

- If you get a message during login that there is an issue with the CSRF token, e.g. `WARNING:django.security.csrf:Forbidden (CSRF token from POST incorrect.): /loginres/` in the log and also i nthe UI, then switch to using `localhost:8000` as the domain, not the localhost IP used in the examples in this README file.

## Contributing

_All contributions are welcome!_
We appreciate any feedback, suggestions, or code contributions to improve the project.
Please follow the [contribution guidelines](CONTRIBUTING.md) for more details.

## Changelog

We operate a [changelog](CHANGELOG.md) to keep track of changes and updates to the project.
The changelog follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format and is versioned according to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is managed in `optimap/__init__.py`.

## Design colours and logos

Optimeta colour = _primary colour_: #158F9B

Complimentary colour for _warnings_, _errors_: #9B2115

Colours for _highlighting_ (split complimentary): #3C159B #9B7115

For future use, optional colours for variation, e.g., for different map features: #158F9B #159B71 #159B8C #158F9B #15749B #15599B

The **logos** and favicon are in the repository in the folder [`works/static/`](https://github.com/GeoinformationSystems/optimap/tree/main/works/static).

## Deployment

OPTIMAP supports two deployment approaches: The app is deployed in the TUD Enterprise Cloud via Docker Compose; see [docs/deploy-docker-compose.md](docs/deploy-docker-compose.md) for the recipe and the `certbot`-based HTTPS setup.
The app is deployed in the TUD Enterprise Cloud via Docker Compose; see [docs/deploy-docker-compose.md](docs/deploy-docker-compose.md) for the recipe and the `certbot`-based HTTPS setup.

### Docker deployment

Containerized deployment using Docker Compose with nginx, Gunicorn, and PostgreSQL/PostGIS.

| File | Purpose |
| ------ | --------- |
| `docker-compose.yml` | Development configuration |
| `docker-compose.deploy.yml` | Production configuration with SSL |
| `etc/nginx.deploy.conf` | nginx reverse proxy with HTTPS |
| `etc/manage-and-run.sh` | Container startup script |

```bash
# Production deployment
docker compose -f docker-compose.deploy.yml up -d
```

HTTPS certificates are managed via certbot container.

### Native deployment

Run OPTIMAP directly on the host system with systemd services, nginx, and a native PostgreSQL/PostGIS database.

| File | Purpose |
| ------ | --------- |
| `docs/deployment-plain.md` | Comprehensive deployment guide |
| `etc/deploy-plain/` | Configuration templates (systemd, nginx, Gunicorn) |
| `etc/deploy-plain/install.sh` | Automated installation script |

```bash
# Run the installation script
sudo ./etc/deploy-plain/install.sh
```

See `docs/deployment-plain.md` for detailed instructions including database setup, SSL configuration, and maintenance procedures.

### Production instance

The app is deployed in the TUD Enterprise Cloud at <https://optimap.geo.tu-dresden.de>.

## Operation

Day-to-day operation of a running OPTIMAP — managing harvesting sources and events, curating collections, blocking abusive users, running the Django-Q cluster, and the rest of the Django-admin surface — is documented in the operator handbook at **[docs/manage.md](docs/manage.md)**.

## License

This software is published under the GNU General Public License v3.0 (see file [`LICENSE`](LICENSE)).
<!-- REUSE-IgnoreStart -->
Licensing is declared per-file following the [REUSE 3.3 specification](https://reuse.software/spec/) — every first-party source file carries an inline `SPDX-License-Identifier: GPL-3.0-or-later` header, and [`REUSE.toml`](REUSE.toml) covers generated files (Django migrations), test fixtures, binary assets (logos, PDFs, fonts), and vendored third-party libraries in `works/static/` (their own upstream licenses apply — see [`LICENSES/LicenseRef-vendored.txt`](LICENSES/LicenseRef-vendored.txt) for the inventory).
<!-- REUSE-IgnoreEnd -->

To verify compliance, run `reuse lint` — see [License headers (REUSE)](#license-headers-reuse) above for the workflow when adding new files.
