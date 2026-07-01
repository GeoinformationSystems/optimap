# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OPTIMAP is a geospatial discovery portal for research articles based on open metadata. Built with Django/GeoDjango and PostgreSQL/PostGIS, it enables users to discover scientific publications through map-based search, temporal filtering, and spatial metadata.

Part of the KOMET project (<https://projects.tib.eu/komet>), continuing from OPTIMETA (<https://projects.tib.eu/optimeta>). Source code and issue tracker: <https://github.com/GeoinformationSystems/optimap>.

## General workflow (every task)

Apply these steps to every task. Most are conditional — check whether the condition is met before acting.

- **Keep a to-do list** for any non-trivial task (multi-step or touching more than one file). Track progress as you go.
- **Run `/simplify`** after completing any non-trivial change (more than 20 lines of code), **before** running the test suite.
- **Run `/code-review`** after completing any complex change set (more than 5 changed files **or** more than 100 lines of code), **before** running the test suite. (For very large or risky changes, run both `/simplify` and `/code-review`.)
- **Run the test suite** after the cleanup/review steps above (see [Testing](#testing)).
- **Always include questions in a plan**, both for clarification (missing requirements, ambiguity) and for judgement calls (design trade-offs where more than one reasonable choice exists). Before presenting a plan via `ExitPlanMode`, re-read this section and fold the applicable steps (to-do list, `/simplify`, `/code-review`, test suite) into the plan itself so they are scheduled, not forgotten.

## Companion docs

- [README.md](README.md) — developer / deployer setup, local dev, harvesting CLI.
- [docs/manage.md](docs/manage.md) — admin / operator handbook (Django admin workflows, harvesting management, suggested sections for the rest of the admin surface). When the user asks about how to run, monitor, or troubleshoot a feature **as an admin**, read this first and update it as features change.
- [CHANGELOG.md](CHANGELOG.md) — Keep-a-Changelog-formatted release notes; update on every user-visible change.
- [docs/geoextent_api.md](docs/geoextent_api.md) — geoextent API endpoints, parameters, and response shapes.

## Core Architecture

### Django Apps Structure

- **optimap/** - Main Django project settings and URL routing
  - `settings.py` - All configuration via environment variables prefixed with `OPTIMAP_`
  - `.env` file for local config (see `.env.example` for all available parameters)

- **works/** - Main application containing all models, views, and business logic
  - **Models** ([models.py](works/models.py)):
    - `Work` - Core model with spatial (`GeometryCollectionField`) and temporal metadata
    - `Source` - OAI-PMH and RSS/Atom harvesting sources with metadata
    - `HarvestingEvent` - Tracks harvesting jobs
    - `Subscription` - User subscriptions with regional filters (continents/oceans), sources, and collections
    - `GlobalRegion` - Predefined geographic regions (continents and oceans) for feeds and subscriptions
    - `CustomUser` - Extended Django user model
    - `UserProfile` - User preferences (notifications, etc.)
    - `EmailLog` - Email notification tracking
    - `WikidataExportLog` - Wikidata/Wikibase export tracking
    - `BlockedEmail`/`BlockedDomain` - Anti-spam mechanisms
    - `ServiceToken` - Generic per-service API credential store (refresh token + cached access token), editable in the Django admin. Currently used for the OpenAIRE refresh-token flow; registry of services in `works/utils/service_tokens.py`
  - **Views** ([views.py](works/views.py)) - Handles passwordless login, subscriptions, data downloads
  - **Harvesting** ([harvesting/](works/harvesting/)) — one module per source type (`oai.py`, `rss.py`, `crossref.py`, `mountain_wetlands.py`, `openalex_source.py`) plus shared helpers (`common.py`, `sessions.py`, `metadata_html.py`) and **enrichment** modules (`openalex.py` and `openaire.py`, coordinated via the fill-if-empty `enrichment.py::apply_enrichment` helper). Public entry points are re-exported from [tasks.py](works/tasks.py) so Django-Q dotted-path schedules keep working. OpenAIRE enrichment runs as an async post-harvest sweep enqueued from `common.py::complete_harvest` (all sources) and via the `enrich_openaire` backfill command.
  - **Other tasks** ([tasks.py](works/tasks.py)) — non-harvest Django-Q tasks: monthly email digest, subscription emails, GeoJSON / GeoPackage cache regeneration, schedule helpers.
  - **API** ([api.py](works/api.py), [viewsets.py](works/viewsets.py), [serializers.py](works/serializers.py)) - DRF REST API at `/api/v1/`
  - **Feeds** ([feeds.py](works/feeds.py), [feeds_geometry.py](works/feeds_geometry.py)) - GeoRSS/GeoAtom feed generation
  - **EO4GEO BoK** ([bok/](works/bok/)) — thin client + cached trimmed snapshot of the [Body of Knowledge](https://eo4geo.eu/bok/) (`client.py`), public autosuggest endpoint at `/api/v1/bok/search/` (`views.py`), code validators (`validators.py`). Cached in the `default` DB cache, lazy on miss; refresh via `python manage.py refresh_bok_snapshot`. See [docs/manage.md](docs/manage.md#eo4geo-bok-snapshot).

### Key Technologies

- **GeoDjango** with **PostGIS** for spatial data (SRID 4326)
- **Django REST Framework** with `rest_framework_gis` for geospatial API
- **Django-Q2** for background task scheduling (harvesting, email notifications, data dumps)
- **drf-spectacular** for OpenAPI schema

### Data Flow

1. **Harvesting**: OAI-PMH sources → `HarvestingEvent` → parse XML → create `Work` records with spatial/temporal metadata
2. **API**: Publications exposed via REST API at `/api/v1/works/` with spatial filtering
3. **Feeds**: Dynamic GeoRSS/GeoAtom feeds filtered by region or global
4. **Data Export**: Scheduled tasks generate cached GeoJSON/GeoPackage dumps in `/tmp/optimap_cache/`

## Development Commands

### Local & Docker development

Full setup — venv, GDAL, PostGIS container, `migrate`, `createcachetable`,
`load_global_regions`, `qcluster`, `generate_pygeoapi_openapi` — is documented in
[README.md](README.md#development). Quick reference:

- **Docker:** `docker compose up` → <http://localhost:80/> (use `localhost`, not
  `127.0.0.1`, to avoid CSRF issues). Migrations auto-apply via `etc/manage-and-run.sh`.
- **Local dev server:** `OPTIMAP_DEBUG=True OPTIMAP_CACHE=dummy python manage.py runserver`
  → http://127.0.0.1:8000/

### Code formatting

[Ruff](https://docs.astral.sh/ruff/) is used for formatting, import sorting, and linting. Configuration is in `pyproject.toml`.

```bash
# Check formatting and lint (what CI runs)
ruff format --check .
ruff check .

# Apply formatting and auto-fixable lint issues
ruff format .
ruff check --fix .
```

**VSCode**: install the [Ruff extension](https://marketplace.visualstudio.com/items?itemName=charliermarsh.ruff) (recommended in `.vscode/extensions.json`); `.vscode/settings.json` enables format-on-save automatically.

**PyCharm**: install the [Ruff plugin](https://plugins.jetbrains.com/plugin/20574-ruff) from JetBrains Marketplace and enable "Run ruff on save". PyCharm reads `pyproject.toml` automatically.

### Testing

All tests are always run using the virtual environment defined in `.venv/`; the Docker config is only for deployment of the the app.

```bash
# Install test dependencies
pip install -r requirements-dev.txt

# Run unit tests (fast — excludes network-dependent tests)
python manage.py test tests --exclude-tag=online

# Run UI tests
python -Wa manage.py test tests-ui

# Test with clean output
OPTIMAP_LOGGING_LEVEL=WARNING python manage.py test tests --exclude-tag=online

# Coverage
coverage run --source='publications' --omit='*/migrations/**' manage.py test tests
coverage report --show-missing --fail-under=70
coverage html  # generates htmlcov/
```

Run a single module/test with `python manage.py test tests.test_geoextent` (or `…test tests-ui`); add `-Wa` to surface deprecation warnings.

**Run at most one test-execution shell at a time.** Never launch a second `manage.py test` (foreground or background) while one is still running — wait for it to finish, check its result, then run the next one. Concurrent test runs against the same `--keepdb` database race and produce unreliable failures, and stacking background shells makes it hard for the user to see what's actually happening or abort cleanly.

#### `online`-tagged tests (network required)

Tests decorated with `@tag('online')` make real HTTP requests to external
services (Copernicus OAI-PMH, GEO-LEO, AGILE-GISS, Zenodo, PANGAEA, etc.).
They live in [tests/test_harvesting.py](tests/test_harvesting.py) and
[tests/test_geoextent.py](tests/test_geoextent.py), and add ~150s+ to a run.
They self-skip when the endpoint is unreachable, but they still spend the
network round-trip, so exclude them by default during iterative development:

```bash
python manage.py test tests --exclude-tag=online   # default dev loop
python manage.py test tests --tag=online           # only the online ones
python manage.py test tests                        # everything (CI does this)
```

**Run the online tests when you change:**

- Anything under [works/harvesting/](works/harvesting/) — OAI-PMH parsing, RSS/Atom,
  Crossref, mountain-wetlands, OpenAlex (both `openalex.py` enrichment and
  `openalex_source.py` as-source harvester), or `common.py`/`sessions.py`/
  `metadata_html.py` helpers. Real endpoints catch schema drift and parser
  regressions that fixtures don't.
- The `harvest_*` task entry points re-exported from [works/tasks.py](works/tasks.py).
- The geoextent remote-extraction code path
  ([works/views.py](works/views.py) `geoextent_extract_remote`, related
  serializers, and the `extract-remote` / `extract-batch` endpoints) — these
  exercise live DOI resolvers and repository APIs.
- HTTP session / retry / timeout configuration shared by the above.

**Separately:** [tests/test_harvesting_online.py](tests/test_harvesting_online.py)
uses `@unittest.skipIf(settings.TEST_HARVESTING_ONLINE != True, …)` instead of
the `online` tag and is gated by the `OPTIMAP_TEST_HARVESTING_ONLINE=True`
environment variable. Set it when you want those legacy live-harvest checks
too:

```bash
OPTIMAP_TEST_HARVESTING_ONLINE=True python manage.py test tests.test_harvesting_online
```

**OpenAIRE-authenticated harvest tests.** Online OpenAIRE-enrichment tests (e.g.
`tests.test_real_harvesting`) hit OpenAIRE's anonymous **60 req/hour** limit and
soft-skip. Pass `OPTIMAP_OPENAIRE_TOKEN` to use the authenticated **7200 req/hour**
path (the test DB is empty, so the sweep can't read the stored `ServiceToken`).
Exchange the stored refresh token for an access token without printing it:

```bash
TOKEN=$(python manage.py shell -c \
  "from works.harvesting.openaire import get_openaire_access_token; print(get_openaire_access_token() or '')" \
  2>/dev/null | tail -1)
OPTIMAP_OPENAIRE_TOKEN="$TOKEN" SKIP_REAL_HARVESTING=0 \
  python manage.py test tests.test_real_harvesting.RealHarvestingTest.test_essoar_record_has_openalex_and_openaire_ids
```

`get_openaire_access_token()` auto-refreshes from the stored refresh token (see
the [OpenAIRE refresh-token flow](docs/manage.md#openaire-enrichment)).

### Django Management Commands

Stock Django commands (`migrate`, `makemigrations`, `createsuperuser`,
`collectstatic`, `dumpdata`/`loaddata`, `shell`, `dbshell`, `test`, …) work as
usual. Only OPTIMAP-specific commands are documented below.

#### Custom OPTIMAP Commands

Located in [works/management/commands/](works/management/commands/)

```bash
# Global regions setup
python manage.py load_global_regions
# Loads predefined continent and ocean geometries into GlobalRegion model
# Required for global feeds and regional subscriptions - run once after initial setup

# Data export scheduling
python manage.py schedule_geojson
# Adds GeoJSON/GeoPackage regeneration task to Django-Q schedule
# Creates recurring task to refresh data dumps every 6 hours

# Regenerate data dumps on-demand (synchronous, no Q cluster needed)
python manage.py regenerate_data_dumps
# Runs the umbrella regen and writes GeoJSON + GeoPackage + CSV to /tmp/optimap_cache/
python manage.py regenerate_data_dumps --format csv
# Restrict to a single format (geojson | gpkg | csv)
python manage.py regenerate_data_dumps --dry-run
# Report what would be regenerated without writing

# Harvest from real sources
python manage.py harvest_sources --list
# Lists all available sources (OAI-PMH, RSS/Atom, Crossref, OpenAlex)
python manage.py harvest_sources --all --max-records 50
# Harvests from all configured sources with record limit
python manage.py harvest_sources --source essd --source geo-leo
# Harvests from specific sources by identifier
# Supports: essd, agile-giss, geo-leo, eartharxiv, scientific-data, essoar, copernicus-gi
python manage.py harvest_sources --source copernicus-gi --max-records 10 --create-sources
# copernicus-gi = "Geoscientific Instrumentation, Methods and Data Systems" (ISSN 2193-0864);
# uses issn_l for Crossref filtering because the journal name contains a comma

# Source synchronization
python manage.py sync_source_metadata
# Syncs metadata from configured OAI-PMH sources; updates Source model from endpoints

# OpenAlex source updates
python manage.py update_openalex_sources
# Fetches and updates Source metadata from the OpenAlex API

# Reset harvest schedules
python manage.py reset_harvest_schedules
# Rebuilds the recurring `Harvest Source <id>` schedules with a deferred
# next_run (and stagger by default), recovering from a state where every
# source's schedule fires at once. Flags: --dry-run, --no-stagger, --clear-manual.

# Clear Django caches — clears configured backends (memory, default, dummy).
python manage.py clear_caches
# Flags: --cache <alias> / --exclude <alias> (repeatable; `--exclude default`
# preserves in-flight login/email-confirmation tokens), --dry-run.
# See docs/manage.md → "Manage data dumps and caches" for what each backend stores.

# Extract BoK concepts from AGILE GISS PDFs (backfill existing works)
python manage.py extract_agile_bok
# Downloads the full-text PDF for each AGILE GISS work and parses the
# "BoK Concepts" section. Skips works that already have bok_concepts set
# unless --force is given. Flags: --limit N, --throttle SECONDS, --force, --dry-run.

# Backfill empty abstract/keywords/authors from OpenAIRE (fill-if-empty, never
# overwrites; records every decision in Work.provenance). New harvests get this
# via an async sweep (OPTIMAP_OPENAIRE_ENRICH_ON_HARVEST).
python manage.py enrich_openaire
# Flags: --collection/--doi-prefix/--source filters, --limit, --throttle, --force,
# --dry-run, --async (enqueues one Django-Q task, works.tasks.enrich_openaire_backfill;
# needs a running qcluster). OPTIMAP_OPENAIRE_TOKEN raises the rate limit 60→7200/hour,
# or store a monthly refresh token in the ServiceToken admin.
# See docs/manage.md → "OpenAIRE enrichment" / "Renewing the OpenAIRE refresh token".

# Link Work.countries via offline point-in-polygon join (issue #261)
python manage.py backfill_work_countries
# Links each work (with geometry but no countries) to every Country whose Natural
# Earth outline intersects its geometry — multi-valued for transboundary works.
# Needs `load_countries` to have populated the Country table. A weekly self-healing
# sweep (works.tasks.backfill_work_countries) does this automatically and emails
# staff on change/error; the post_save signal handles it on save (gated by
# OPTIMAP_GEOCODE_WORKS_ON_SAVE). country_code (scalar) was replaced by the
# Work.countries M2M; the API exposes `country_codes` (list). Flags: --limit, --dry-run.
# `backfill_placenames` now fills only Work.placename (Nominatim), not country.

# Generate pygeoapi OpenAPI document (required for /ogcapi/ endpoint)
python manage.py generate_pygeoapi_openapi
# Reads etc/pygeoapi-config.yml and writes etc/pygeoapi-openapi.yml.
# The /ogcapi/ endpoint is silently disabled until this file exists. Run with
# --force to regenerate; Docker startup (etc/manage-and-run.sh) runs it --force on deploy.
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

# Manually regenerate GeoJSON/GeoPackage cache (synchronous, without Django-Q)
python manage.py shell -c "from works.tasks import regenerate_geojson_cache; regenerate_geojson_cache()"
```

## Static assets — no CDN

**All JavaScript and CSS libraries must be served from `works/static/`, never from a CDN.**
See [`works/static/README.md`](works/static/README.md) for the full list and provenance of every
vendored file, and [`works/static/download_libraries.sh`](works/static/download_libraries.sh)
for how to (re-)download them.

When adding a new JS or CSS dependency:
1. Download the file(s) with `wget` into `works/static/js/` or `works/static/css/`.
2. Reference them in templates with `{% static 'js/…' %}` / `{% static 'css/…' %}`.
3. Add an entry to `works/static/README.md` (provenance, version, license, homepage).
4. Add the `wget` line(s) to `works/static/download_libraries.sh` so the file can be refreshed.

Do **not** add `<script src="https://unpkg.com/…">`, `<link href="https://cdn…">`, or any other
external URL for JS/CSS assets — not even "temporarily" or "for now".

## Important Patterns

### Configuration

All deployment-specific config uses `OPTIMAP_*` environment variables loaded from `.env` or environment. See [optimap/.env.example](optimap/.env.example).

### Spatial Data

- All geometries use `GeometryCollectionField` with SRID 4326
- WKT format for manual geometry input (use https://wktmap.com/ for creation)
- Spatial metadata extracted from HTML `<meta name="DC.SpatialCoverage">` tags during harvesting

### Harvesting Flow

1. Create/configure `Source` in admin with OAI-PMH URL, RSS/Atom feed URL, Crossref prefix, MaRESS API URL, or OpenAlex source identifier (`S<digits>` on `openalex_id`; the public `openalex_url` is now a derived property).
2. Django-Q task creates `HarvestingEvent` (or use `harvest_sources` command for direct harvesting)
3. Fetch XML/RSS/JSON → parse → extract DOI, spatial, temporal metadata → save `Work` records with status `h` (Harvested)
4. Track status in `HarvestingEvent.status` (pending/in_progress/completed/failed)
5. Works with spatial/temporal metadata can be published directly, or users can contribute missing metadata
6. OpenAlex enrichment fills extra metadata (authors, keywords, topics) when a DOI is present, via `works.harvesting.openalex.build_openalex_fields` inside the harvester. This is distinct from the **`openalex` source type** (`works.harvesting.openalex_source`), which uses OpenAlex as the *primary* harvest origin (see [docs/manage.md](docs/manage.md) → "OpenAlex-as-source").

### Authentication

- Passwordless "magic link" system based on own implementation
- Users receive login token via email (10-minute expiration)
- Email confirmation for account changes

### Email notifications

All outgoing emails use file-based plain-text templates in `works/templates/email/`. Adding a new email notification follows a fixed pattern:

**1. Create the template** at `works/templates/email/<name>.en.txt`. The **first line is the subject**, a **blank line separates it from the body**. Subjects use `[OPTIMAP]` prefix and an emoji:

```
[OPTIMAP] 🔔 Something happened — {{ title }}

Hello {{ username }},

Here is the detail: {{ detail_url }}
```

Autoescape is disabled (see `works/utils/email.py`), so URLs with `&` render correctly without `&amp;`.

**2. Render the template** using the shared helper:

```python
from works.utils.email import render_email

subject, body = render_email('email/<name>.en.txt', {
    'title': work.title,
    'detail_url': absolute_url,
})
send_mail(subject, body, settings.EMAIL_HOST_USER, [recipient])
```

For harvest completion/failure emails use `render_harvest_email` from `works.harvesting.common` (same helper, re-exported for convenience).

**3. Queue it** via `django_q.tasks.async_task` for any email that is not a direct user-action response (i.e. everything except magic-link and email-change confirmation). This keeps request latency low and survives SMTP hiccups.

**4. Write a content assertion test.** Every email must have at least one test that checks a key substring in `mail.outbox[0].body` — not just that an email was sent. See `tests/test_auth_emails.py`, `tests/test_work_notifications.py`, and `tests/test_regular_harvesting.py` for examples. Use `@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")`.

**Complete email inventory** (15 templates, 23 distinct sends):

| Template | Trigger | Sender |
|----------|---------|--------|
| `harvest_success.en.txt` | Harvest completes (OAI, RSS, Crossref, MaRESS, OpenAlex) | `render_harvest_email` in each harvester |
| `harvest_failure.en.txt` | Harvest fails (same 5 harvesters) | same |
| `magic_link.en.txt` | User requests login | `works/views/auth.py::loginres` (synchronous) |
| `email_change_confirm.en.txt` | User requests email change | `works/views/auth.py::change_useremail` |
| `email_changed_notify.en.txt` | Email change confirmed | `works/views/auth.py::confirm_email_change` |
| `account_deletion_confirm.en.txt` | User requests account deletion | `works/views/auth.py::request_delete` |
| `contribution_review.en.txt` | Work contributed — notifies admins/curators | `works/notifications.py` via Django-Q |
| `publication_to_contributor.en.txt` | Work published — notifies contributor | same |
| `curator_change.en.txt` | Curator added/removed from collection | same |
| `new_user_admin.en.txt` | New user confirmed first login | same |
| `monthly_digest.en.txt` | Scheduled monthly digest | `works/tasks.py::send_monthly_email` |
| `subscription_regional.en.txt` | Scheduled regional subscription digest | `works/tasks.py::send_subscription_based_email` |
| `service_token_renewal.en.txt` | Service refresh token (OpenAIRE) nears monthly expiry — notifies staff | `works/tasks.py::check_service_token_renewals` (weekly) |
| `openaire_enrich_aborted.en.txt` | OpenAIRE sweep/backfill aborts after repeated systemic failures — notifies staff | `works/harvesting/openaire.py::notify_openaire_abort` |
| `author_orcid_backfill_aborted.en.txt` | Author ORCID backfill (`--async`) aborts after repeated systemic OpenAlex/OpenAIRE failures — notifies staff | `works/tasks.py::notify_backfill_abort` |

**Opt-out**: work-event emails (contribution/publish) respect `UserProfile.notify_work_events` (opt-out, default True). Monthly digest respects `UserProfile.notify_new_manuscripts`. Blocked senders are checked via `BlockedEmail`/`BlockedDomain`. All sends are logged to `EmailLog` for audit (harvest emails are the exception).

**Future i18n**: swap `.en.txt` for `.de.txt` etc. and pick the template name based on the user's locale — no other code change needed.

### Testing Notes

- UI tests use Helium/Selenium (set `headless=False` for debugging)
- UI tests use Django cache for token management (see test_emailchange.py, test_accountdeletion.py, test_loginresponse.py)
- Tests create mock tokens in `setUp()` and retrieve them from cache during test execution
- Test data fixtures in `fixtures/` directory
- Use `-Wa` flag to show deprecation warnings

## Common Gotchas

- **CSRF errors during login**: Switch to `localhost:8000` instead of `127.0.0.1:8000`
- **Migrations on startup**: Applied automatically via `etc/manage-and-run.sh` in Docker
- **Debug mode**: Default is `OPTIMAP_DEBUG=False` - set explicitly for development
- **Email debugging**: Set `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` in `.env`
- **Django-Q cluster**: Must be running separately for harvesting/scheduled tasks to execute
- **Data dumps retention**: Controlled by `OPTIMAP_DATA_DUMP_RETENTION` (default: 3)
- **Formatter not installed**: Run `pip install -r requirements-dev.txt`; verify with `ruff format --check .` and `ruff check .`.
- **Django template comments `{# … #}` must be on a single line.** The parser does not support multi-line `{# #}` blocks — a newline inside the comment is rendered verbatim and may break the page. For multi-line notes use `{% comment %}…{% endcomment %}` instead.

## Key Features & UI

- **Navigation:** burger menu (☰, links to all pages), user menu (login/logout/settings), footer (sitemap, about/contact, privacy, data license).
- **Map (Leaflet):** publication markers, zoom-to-all control (all maps), gazetteer search (Nominatim/GeoNames/Photon), toggle-able global-regions overlay, paginated popups for overlapping works, geometry highlighting on selection.
- **Workflow:** publication status Draft → Harvested → Contributed → Published (plus Testing, Withdrawn). Users contribute spatial/temporal extent to harvested works; admins review and publish. Regional subscriptions email users about new works in selected continents/oceans.

### Pages

- `/` - Main map and timeline of publications
- `/works/list/` - Browse all works with pagination (configurable page size)
- `/work/<id>/` or `/work/<doi>/` - Individual work landing page
- `/contribute/` - Crowdsourced spatial/temporal metadata contribution (paginated)
- `/subscriptions/` - Regional subscription management (continents and oceans)
- `/geoextent/` - Geoextent extraction web UI
- `/pages` - Human-readable sitemap with organized page list
- `/feeds/` - Feed landing pages for global and regional RSS/Atom feeds
- `/ogcapi/` - OGC API - Features landing page (pygeoapi). Only active when `etc/pygeoapi-openapi.yml` exists — generate with `python manage.py generate_pygeoapi_openapi`. See [docs/ogcapi-clients.md](docs/ogcapi-clients.md) for Python/R/QGIS usage examples.

## API & Endpoints

> **Keep the API docs in sync with the code.** Whenever you add, remove, or change a REST
> endpoint — including new `@action` methods, new ViewSets, new function-based `@api_view`
> handlers, new query parameters, new response shapes, or new error paths — also update
> the schema annotations so `/api/schema/ui/` keeps reflecting reality. Concretely:
> - Decorate every public endpoint with `@extend_schema(summary=…, tags=[…], request=…, responses={200: …, 4xx/5xx: OpenApiResponse(…)})`. Use `@extend_schema_view` on ViewSets to set per-method summaries.
> - Tag each endpoint with one of the `TAGS` declared in `optimap/settings.py:SPECTACULAR_SETTINGS` (Works / Sources / Subscriptions / Global regions / Geoextent / Gazetteer / Downloads). If a new endpoint doesn't fit any tag, add a tag entry alongside the others so Redoc gives it a sidebar section.
> - Document every error status the view can actually return — `404`, `400`, `401`/`403`, `413`, `500`, etc. (cross-check against `tests/test_*.py` assertions on `response.status_code` and explicit `Response(..., status=...)` returns in the view).
> - For function-based Django views that should appear in the docs (downloads, gazetteer proxies, …), wrap them in `@api_view([…])` + `@permission_classes([...])` so drf-spectacular can pick them up.
> - Run `python manage.py spectacular --file /tmp/optimap_schema.yaml` after the change; it must report `Errors: 0` (warnings are tolerable but should not regress).
> - Update the Markdown intro in `SPECTACULAR_SETTINGS['DESCRIPTION']` (and the relevant `TAGS` description) when conventions change (auth, pagination, filtering, new endpoint families).
> - **Provenance endpoint** (`GET /api/v1/works/<id>/provenance/`, `WorkViewSet.provenance` in [works/viewsets.py](works/viewsets.py)): the `@extend_schema` decorator contains the authoritative list of `metadata_sources` keys and values, `harvest` keys, `openalex_match` status values, event types, and response examples. Whenever you add a new key to `Work.provenance` (in any harvester, view, or utility), or change the set of possible values for an existing key, update all of the following in the same commit: the `description` tables in `@extend_schema`, the `help_text` on the affected `inline_serializer` field, the `OpenApiExample` values, and the schema quick reference in [docs/manage.md](docs/manage.md#work-provenance). The provenance schema is defined in [works/utils/provenance.py](works/utils/provenance.py).

- `/api/v1/` - REST API root (see `/api/schema/ui/` for OpenAPI docs)
- `/api/v1/works/contribute-doi/` - POST (auth required) to add a new work by DOI; harvests Crossref + enrichment synchronously, returns existing-vs-created. See [docs/manage.md](docs/manage.md#user-contributions-by-doi)
- `/admin/` - Django admin interface
- `/download/geojson/` - Download full publication dataset as GeoJSON
- `/download/geopackage/` - Download as GeoPackage
- `/download/csv/` - Download as CSV (one row per work, `WKT` geometry column in OGC Simple Features)
- `/feed/georss/` - Global GeoRSS feed
- `/feeds/georss/<slug>/` - Region-filtered GeoRSS feed (continents and oceans)
- `/sitemap-works.xml` - Sitemap for all published works
- `/sitemap-feeds.xml` - Sitemap for all regional feeds
- `/geoextent/` - Geoextent extraction web UI (interactive tool for file upload and remote resource extraction)
- `/ogcapi/collections/works/items` - Published works via OGC API - Features (pygeoapi); supports `bbox`, `datetime`, `limit`/`offset` — see [docs/ogcapi-clients.md](docs/ogcapi-clients.md)

### Geoextent API & Web UI

Full endpoint, parameter, response-format (`geojson`/`wkt`/`wkb`), and status-code
reference: [docs/geoextent_api.md](docs/geoextent_api.md). The endpoints are public
(no auth) and return GeoJSON FeatureCollections by default.

- `POST /api/v1/geoextent/extract/` — extent from an uploaded file
- `GET|POST /api/v1/geoextent/extract-remote/` — extent from remote repositories
  (Zenodo, PANGAEA, OSF, Figshare, Dryad, GFZ Data Services, Dataverse); parallel
  downloads via the `GEOEXTENT_DOWNLOAD_WORKERS` setting
- `POST /api/v1/geoextent/extract-batch/` — combined extent over multiple files
- `POST /api/v1/geoextent/extract-text/` — NER place-name extraction from free text
  (issue #199); see [docs/ner_location_suggestions.md](docs/ner_location_suggestions.md)

**Known bug (upstream):** `geoextent.from_remote()` returns the bounding box as
`[minLat, minLon, maxLat, maxLon]` instead of the GeoJSON-standard
`[minLon, minLat, maxLon, maxLat]`. Affects remote extraction only (not file
uploads); must be fixed upstream in the geoextent library.

**Web UI** ([/geoextent](works/templates/geoextent.html), `geoextent()` in
[works/views.py](works/views.py); UI tests
[tests-ui/test_geoextent.py](tests-ui/test_geoextent.py)): supported formats and
providers load dynamically via `geoextent.lib.features.get_supported_features()` —
never hardcode format lists. Use **sentence case** for all headlines and fields.
Upload size limits come from the `GEOEXTENT_MAX_FILE_SIZE_MB` /
`GEOEXTENT_MAX_BATCH_SIZE_MB` / `GEOEXTENT_MAX_DOWNLOAD_SIZE_MB` settings.

## Version Management

Version is maintained in [optimap/\_\_init\_\_.py](optimap/__init__.py). Follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Update [CHANGELOG.md](CHANGELOG.md) following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

### Release procedure

1. **Bump the version** in `optimap/__init__.py` (minor for new features, patch for bug-fix-only).
2. **Update CHANGELOG.md**: rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD` and add a fresh empty `[Unreleased]` section above it.
3. **Commit** both files: `git commit -m "bump version to X.Y.Z"`.
4. **Tag** the commit: `git tag -a vX.Y.Z -m "Release vX.Y.Z"`.
5. **Push** commits and tag: `git push && git push origin vX.Y.Z`.
6. **Verify** that both the commit and tag are visible on the upstream repository before proceeding:
   ```bash
   gh api repos/GeoinformationSystems/optimap/git/refs/tags/vX.Y.Z --jq '.object.sha'
   # must match: git rev-parse vX.Y.Z^{}
   git log --oneline origin/main..main
   # must print nothing (no local-only commits)
   ```
7. **Create the GitHub release**:
   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <(sed -n '/^\#\# \[X.Y.Z\]/,/^\#\# \[/p' CHANGELOG.md | head -n -1)
   ```
   Or via the GitHub UI using the CHANGELOG section as release notes.
