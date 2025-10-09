# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OPTIMAP is a geospatial discovery portal for research articles based on open metadata. Built with Django/GeoDjango and PostgreSQL/PostGIS, it enables users to discover scientific publications through map-based search, temporal filtering, and spatial metadata.

Part of the KOMET project (<https://projects.tib.eu/komet>), continuing from OPTIMETA (<https://projects.tib.eu/optimeta>).

## Core Architecture

### Django Apps Structure

- **optimap/** - Main Django project settings and URL routing
  - `settings.py` - All configuration via environment variables prefixed with `OPTIMAP_`
  - `.env` file for local config (see `.env.example` for all available parameters)

- **publications/** - Main application containing all models, views, and business logic
  - **Models** ([models.py](publications/models.py)):
    - `Publication` - Core model with spatial (`GeometryCollectionField`) and temporal metadata
    - `Source` - OAI-PMH harvesting sources
    - `HarvestingEvent` - Tracks harvesting jobs
    - `Subscription` - User subscriptions with spatial/temporal filters
    - `CustomUser` - Extended Django user model
    - `BlockedEmail`/`BlockedDomain` - Anti-spam mechanisms
  - **Views** ([views.py](publications/views.py)) - Handles passwordless login, subscriptions, data downloads
  - **Tasks** ([tasks.py](publications/tasks.py)) - Django-Q async tasks for harvesting and data export
  - **API** ([api.py](publications/api.py), [viewsets.py](publications/viewsets.py), [serializers.py](publications/serializers.py)) - DRF REST API at `/api/v1/`
  - **Feeds** ([feeds.py](publications/feeds.py), [feeds_geometry.py](publications/feeds_geometry.py)) - GeoRSS/GeoAtom feed generation

### Key Technologies

- **GeoDjango** with **PostGIS** for spatial data (SRID 4326)
- **Django REST Framework** with `rest_framework_gis` for geospatial API
- **Django-Q2** for background task scheduling (harvesting, email notifications, data dumps)
- **drf-spectacular** for OpenAPI schema

### Data Flow

1. **Harvesting**: OAI-PMH sources → `HarvestingEvent` → parse XML → create `Publication` records with spatial/temporal metadata
2. **API**: Publications exposed via REST API at `/api/v1/publications/` with spatial filtering
3. **Feeds**: Dynamic GeoRSS/GeoAtom feeds filtered by region or global
4. **Data Export**: Scheduled tasks generate cached GeoJSON/GeoPackage dumps in `/tmp/optimap_cache/`

## Development Commands

### Docker Development

```bash
# Start all services (app, db, webserver)
docker compose up

# Load test data
docker compose run --entrypoint python app manage.py loaddata fixtures/test_data.json

# Create superuser
docker compose run --entrypoint python app manage.py createsuperuser

# Run migrations manually (normally auto-applied via etc/manage-and-run.sh)
docker compose run --entrypoint python app manage.py migrate

# Collect static files
docker compose run --entrypoint python app manage.py collectstatic --noinput
```

Access at <http://localhost:80/> (note: use `localhost` not `127.0.0.1` to avoid CSRF issues)

### Local Development

```bash
# Setup (once)
python -m venv .venv
source .venv/bin/activate
pip install gdal=="$(gdal-config --version).*"
pip install -r requirements.txt

# Start local PostGIS container
docker run --name optimapDB -p 5432:5432 \
  -e POSTGRES_USER=optimap -e POSTGRES_PASSWORD=optimap \
  -e POSTGRES_DB=optimap -d postgis/postgis:14-3.3

# Apply migrations
python manage.py migrate
python manage.py createcachetable

# Load global regions (required for predefined feeds)
python manage.py load_global_regions

# Start Django-Q cluster (separate terminal, required for harvesting/tasks)
python manage.py qcluster

# Run server (debug mode)
OPTIMAP_DEBUG=True OPTIMAP_CACHE=dummy python manage.py runserver
```

Access at http://127.0.0.1:8000/

### Testing

```bash
# Install test dependencies
pip install -r requirements-dev.txt

# Run unit tests
python manage.py test tests

# Run UI tests (requires docker compose up or runserver)
python -Wa manage.py test tests-ui

# Test with clean output
OPTIMAP_LOGGING_LEVEL=WARNING python manage.py test tests

# Coverage
coverage run --source='publications' --omit='*/migrations/**' manage.py test tests
coverage report --show-missing --fail-under=70
coverage html  # generates htmlcov/
```

### Django Management Commands

#### Standard Django Commands

```bash
# Database operations
python manage.py makemigrations              # Create new migrations (should detect no changes normally)
python manage.py migrate                     # Apply database migrations
python manage.py showmigrations              # List all migrations and their status
python manage.py sqlmigrate publications 0001  # Show SQL for a specific migration

# User management
python manage.py createsuperuser             # Create admin user interactively
python manage.py createsuperuser --username=optimap --email=admin@optimap.science
python manage.py changepassword <username>   # Change user password

# Static files
python manage.py collectstatic --noinput     # Collect static files to STATIC_ROOT
python manage.py findstatic <filename>       # Find location of static file

# Cache
python manage.py createcachetable            # Create database cache table (required on setup)

# Data management
python manage.py dumpdata <app.Model>        # Export data as JSON
python manage.py loaddata <fixture.json>     # Import data from JSON fixture
python manage.py flush                       # Clear all data from database (careful!)

# Shell access
python manage.py shell                       # Django shell with models loaded
python manage.py shell -c "from publications.tasks import regenerate_geojson_cache; regenerate_geojson_cache()"
python manage.py dbshell                     # Direct PostgreSQL shell

# Development server
python manage.py runserver                   # Start dev server on 127.0.0.1:8000
python manage.py runserver 0.0.0.0:8000     # Start on all interfaces (Docker)
OPTIMAP_DEBUG=True python manage.py runserver  # With debug mode

# Testing
python manage.py test                        # Run all tests
python manage.py test tests                  # Run unit tests only
python manage.py test tests-ui               # Run UI tests only
python manage.py test tests.test_geo_data    # Run specific test module
python manage.py test tests.test_geoextent   # Run geoextent API integration tests
python -Wa manage.py test                    # Show deprecation warnings
```

#### Custom OPTIMAP Commands

Located in [publications/management/commands/](publications/management/commands/)

```bash
# Global regions setup
python manage.py load_global_regions
# Loads predefined continent and ocean geometries into GlobalRegion model
# Required for global feeds functionality - run once after initial setup

# Data export scheduling
python manage.py schedule_geojson
# Adds GeoJSON/GeoPackage regeneration task to Django-Q schedule
# Creates recurring task to refresh data dumps every 6 hours

# Source synchronization
python manage.py sync_source_metadata
# Syncs metadata from configured OAI-PMH sources
# Updates Source model with latest information from endpoints

# OpenAlex journal updates
python manage.py update_openalex_journals
# Fetches and updates journal metadata from OpenAlex API
# Enriches Source records with additional journal information
```

#### Django-Q Task Management

```bash
# Start task worker (required for async operations)
python manage.py qcluster
# Runs background worker to process harvesting jobs, email sending, data exports
# Keep running in separate terminal during development

# Monitor tasks
python manage.py qmonitor      # Live dashboard of task queue
python manage.py qinfo         # Show cluster statistics and status

# Manual task management via Django shell
python manage.py shell
>>> from django_q.models import Schedule
>>> Schedule.objects.all()  # List scheduled tasks
>>> from django_q.tasks import async_task
>>> async_task('publications.tasks.regenerate_geojson_cache')  # Queue a task
```

### Manual Data Operations

```bash
# Create test data dump
python manage.py dumpdata --exclude=auth --exclude=contenttypes | jq > fixtures/test_data.json

# Load fixtures
python manage.py loaddata fixtures/test_data_optimap.json
python manage.py loaddata fixtures/test_data_partners.json
python manage.py loaddata fixtures/test_data_global_feeds.json

# Manually regenerate GeoJSON/GeoPackage cache (without Django-Q)
python manage.py shell -c "from publications.tasks import regenerate_geojson_cache; regenerate_geojson_cache()"
```

## Important Patterns

### Configuration

All deployment-specific config uses `OPTIMAP_*` environment variables loaded from `.env` or environment. See [optimap/.env.example](optimap/.env.example).

### Spatial Data

- All geometries use `GeometryCollectionField` with SRID 4326
- WKT format for manual geometry input (use https://wktmap.com/ for creation)
- Spatial metadata extracted from HTML `<meta name="DC.SpatialCoverage">` tags during harvesting

### Harvesting Flow

1. Create/configure `Source` in admin with OAI-PMH URL
2. Django-Q task creates `HarvestingEvent`
3. Fetch XML → parse → extract DOI, spatial, temporal metadata → save `Publication` records
4. Track status in `HarvestingEvent.status` (pending/in_progress/completed/failed)

### Authentication

- Passwordless "magic link" system based on own implementation
- Users receive login token via email (10-minute expiration)
- Email confirmation for account changes
- CSRF tokens required - use `localhost` domain during development (not 127.0.0.1)

### Testing Notes

- UI tests use Helium/Selenium (set `headless=False` for debugging)
- Test data fixtures in `fixtures/` directory
- Use `-Wa` flag to show deprecation warnings

## Common Gotchas

- **CSRF errors during login**: Switch to `localhost:8000` instead of `127.0.0.1:8000`
- **Migrations on startup**: Applied automatically via `etc/manage-and-run.sh` in Docker
- **Debug mode**: Default is `OPTIMAP_DEBUG=False` - set explicitly for development
- **Email debugging**: Set `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` in `.env`
- **Django-Q cluster**: Must be running separately for harvesting/scheduled tasks to execute
- **Data dumps retention**: Controlled by `OPTIMAP_DATA_DUMP_RETENTION` (default: 3)

## File Structure Highlights

```
optimap/
├── optimap/          # Django project settings
├── publications/     # Main app (models, views, tasks, API)
│   ├── management/commands/  # Custom Django commands
│   ├── static/       # Frontend assets, logos
│   └── templates/    # Django templates
├── tests/            # Unit tests
├── tests-ui/         # Selenium UI tests
├── fixtures/         # Test data JSON
├── etc/              # Deployment scripts (manage-and-run.sh)
├── static/           # Collected static files (generated)
└── docker-compose.yml / docker-compose.deploy.yml
```

## API & Endpoints

- `/api/v1/` - REST API root (see `/api/schema/ui/` for OpenAPI docs)
- `/admin/` - Django admin interface
- `/download/geojson/` - Download full publication dataset as GeoJSON
- `/download/geopackage/` - Download as GeoPackage
- `/feed/georss/` - Global GeoRSS feed
- `/feeds/georss/<slug>/` - Region-filtered GeoRSS feed
- `/geoextent/` - Geoextent extraction web UI (interactive tool for file upload and remote resource extraction)

### Geoextent API Endpoints

#### Public API - No authentication required

All geoextent endpoints return valid GeoJSON FeatureCollections by default, matching the geoextent CLI output format.

- `/api/v1/geoextent/extract/` - Extract spatial/temporal extent from uploaded file
  - Method: POST with multipart/form-data
  - Parameters: file, bbox, tbox, convex_hull, response_format, placename, gazetteer
  - Returns: GeoJSON FeatureCollection with `geoextent_extraction` metadata

- `/api/v1/geoextent/extract-remote/` - Extract extent from remote repositories
  - Methods: GET or POST (same URL)
  - POST: JSON body with `identifiers` array
  - GET: URL parameters with comma-separated `identifiers`
  - Supports: Zenodo, PANGAEA, OSF, Figshare, Dryad, GFZ Data Services, Dataverse
  - Parameters: identifiers, bbox, tbox, convex_hull, response_format, placename, gazetteer, file_limit, size_limit_mb
  - Uses geoextent's native multi-identifier support with automatic extent merging
  - Parallel downloads controlled by `GEOEXTENT_DOWNLOAD_WORKERS` setting
  - Example GET: `/api/v1/geoextent/extract-remote/?identifiers=10.5281/zenodo.4593540&bbox=true&tbox=true`
  - Example POST: `{"identifiers": ["10.5281/zenodo.4593540"], "bbox": true, "tbox": true}`

- `/api/v1/geoextent/extract-batch/` - Batch processing of multiple files
  - Method: POST with multipart/form-data (multiple files)
  - Parameters: files[], bbox, tbox, convex_hull, response_format, placename, gazetteer, size_limit_mb
  - Uses geoextent's `fromDirectory` for native extent combination
  - Returns: GeoJSON FeatureCollection with combined extent and individual features

**Response Formats** (`response_format` parameter):

- `geojson` (default) - Valid GeoJSON FeatureCollection matching CLI output
  - Structure: `{"type": "FeatureCollection", "features": [...], "geoextent_extraction": {...}}`
  - Temporal extent in feature properties as `tbox` (not `temporal_extent`)
- `wkt` - WKT (Well-Known Text) string with metadata
  - Structure: `{"wkt": "POLYGON(...)", "crs": "EPSG:4326", "tbox": [...], "geoextent_extraction": {...}}`
- `wkb` - WKB (Well-Known Binary) hex string with metadata
  - Structure: `{"wkb": "0103...", "crs": "EPSG:4326", "tbox": [...], "geoextent_extraction": {...}}`

See [docs/geoextent_response_formats.md](docs/geoextent_response_formats.md) for detailed examples.

**Metadata Structure** (`geoextent_extraction`):

Property names match geoextent CLI output to avoid confusion:

- `version` - Geoextent library version
- `inputs` - List of input identifiers/filenames
- `statistics.files_processed` - Number of files processed
- `statistics.files_with_extent` - Number of files with valid extent
- `statistics.total_size` - Total size (e.g., "2.71 MiB")
- `format` - Source format (e.g., "remote", "geojson")
- `crs` - Coordinate reference system
- `extent_type` - "bounding_box" or "convex_hull"

**HTTP Status Codes:**

- `200 OK` - Successful extraction
- `400 Bad Request` - Invalid parameters
- `413 Request Entity Too Large` - File too large
- `500 Internal Server Error` - Processing error

Error responses: `{"error": "message"}` (no `success: false` property)

**Supported Input Formats:**
GeoJSON, GeoTIFF, Shapefile, GeoPackage, KML, GML, GPX, FlatGeobuf, CSV (with lat/lon)

**Gazetteers:** Nominatim (default), GeoNames (requires username), Photon

**Known Issues:**

- **Coordinate order bug in geoextent.fromRemote()**: The geoextent library's `fromRemote()` function returns bounding boxes in `[minLat, minLon, maxLat, maxLon]` format instead of the GeoJSON standard `[minLon, minLat, maxLon, maxLat]`. This affects remote extractions only (not file uploads). This needs to be fixed upstream in the geoextent library. Until fixed, remote extraction coordinates will be in the wrong order.

### Geoextent Web UI

Interactive web interface at [/geoextent](publications/templates/geoextent.html) for extracting geospatial/temporal extents from data files.

**Features:**

- File upload (single or batch) with size validation
- Remote resource extraction via DOI/URL (comma-separated)
- Interactive Leaflet map preview with clickable features
- Parameter customization (bbox, tbox, convex_hull, placename, gazetteer)
- Response format selection (GeoJSON, WKT, WKB)
- Download results in selected format
- Client-side file size validation against server limits
- Error handling with informative messages
- Documentation section with supported formats and providers
- Use *sentence case* for all headlines and fields

**Implementation:**

- View: [publications/views.py](publications/views.py) - `geoextent()` function
  - Uses `geoextent.lib.features.get_supported_features()` to dynamically load supported formats and providers
  - No hardcoded format lists - always reflects current geoextent capabilities
- Template: [publications/templates/geoextent.html](publications/templates/geoextent.html)
  - Uses Fetch API for AJAX requests (jQuery slim doesn't include $.ajax)
  - Interactive file management with add/remove functionality
  - Multiple file selection from different locations
  - CSRF token handling for secure POST requests
- Uses existing jQuery (slim) and Bootstrap (no additional libraries)
- Map integration via existing Leaflet setup
- API calls to `/api/v1/geoextent/` endpoints
- UI tests: [tests-ui/test_geoextent.py](tests-ui/test_geoextent.py)

**Configuration:**

Size limits passed from Django settings:

- `GEOEXTENT_MAX_FILE_SIZE_MB` - Single file upload limit
- `GEOEXTENT_MAX_BATCH_SIZE_MB` - Total batch upload limit
- `GEOEXTENT_MAX_DOWNLOAD_SIZE_MB` - Remote resource download limit

**Navigation:**

- Footer link added to [publications/templates/footer.html](publications/templates/footer.html)
- URL route: `path("geoextent/", views.geoextent, name="geoextent")` in [publications/urls.py](publications/urls.py)

## Version Management

Version is maintained in [optimap/\_\_init\_\_.py](optimap/__init__.py). Follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Update [CHANGELOG.md](CHANGELOG.md) following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.
