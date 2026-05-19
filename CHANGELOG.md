# Changelog

All notable changes to OPTIMAP are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Zenodo data archival groundwork** (issue #63) — `python manage.py render_zenodo` builds `README.md`, a versioned `optimap-main.zip` (current git `HEAD`), and a `zenodo_dynamic.json` payload under `data/`; `deposit_zenodo` (or the combined `zenodo_deposit`) updates an existing Zenodo draft via [`zenodo-client`](https://pypi.org/project/zenodo-client/) and never publishes automatically. Each run records a `ZenodoDepositionLog` row (status, file list, total size, DOI, draft URL) and emails all `is_staff` users the outcome with a direct link to the draft. An admin action *Trigger Zenodo Deposition* runs the full render+deposit cycle. The `/data/` page now shows the latest successful deposition (sandbox-aware in `DEBUG`, production-only otherwise). Settings: `ZENODO_API_TOKEN`, `ZENODO_SANDBOX_DEPOSITION_ID`, `ZENODO_API_BASE`. Sources, related-identifier URLs, funding metadata, and the codebook are wired up incrementally in follow-up commits.
- **Zenodo deposition is now fully self-sufficient** (issue #63 closes the "write code to create a new deposition" item). The deposit step bootstraps a fresh draft via `POST /deposit/depositions` when no `ZENODO_SANDBOX_DEPOSITION_ID` is configured and no prior successful `ZenodoDepositionLog` exists; otherwise it reuses the latest log row's ID so re-triggered runs land on the same draft without manual env edits. When the targeted record has been manually published (`submitted=true` + `state="done"`), the next run calls `POST .../actions/newversion`, follows `links.latest_draft`, and updates *that* draft instead — so the second and later deposit cycles work end-to-end without operator intervention. Publishing remains manual.
- **Annual Django-Q schedule for Zenodo deposition** — `python manage.py schedule_zenodo_deposit` registers `works.tasks.run_zenodo_deposition` to run yearly on Dec 31 23:59 (idempotent; safe to re-run). The task chains `regenerate_all_data_dumps` → `render_zenodo_package` → `deposit_to_zenodo` so the deposit always reflects the latest data.
- **Tag works with EO4GEO Body of Knowledge concepts** (closes #245). New `bok_concepts` field on `Work` plus an autosuggest combobox on the work landing page (≥3-character query, full keyboard, multi-select) backed by `GET /api/v1/bok/search/`. Tagged concepts render as chips that link to the canonical concept page on `bok.eo4geo.eu`, surface in the public Work API as `bok_concepts` / `bok_concepts_resolved`, and emit JSON-LD `about: [DefinedTerm,…]` on the landing page. Adding the first concept on a harvested work flips its status from Harvested to Contributed for admin review; Recognition Board credit is recorded under a new generic *Ontology contributions* kind (so the same bucket can later cover other controlled vocabularies) and deduped per (user, work) so the same user adding more concepts later does not double-count. The cached BoK snapshot is refreshed by `python manage.py refresh_bok_snapshot` (pinned to `v3` by default; configurable via `OPTIMAP_BOK_VERSION`). The editor is **opt-in**: set `OPTIMAP_BOK_ENABLED_COLLECTIONS` to a comma-separated list of `Collection.identifier` slugs to enable it on works in those collections — empty (default) disables the editor site-wide. Read-only chips on already-tagged works remain visible regardless.

### Changed

- **Contribution editor open to logged-in users on both Harvested *and* Contributed works** so a second contributor can fill a different gap (e.g. add temporal extent after someone else added a geometry). Pre-existing extents no longer close the form: user B may replace user A's geometry, with the provenance log recording attribution.
- **Recognition Board credit dedupes per (user, work, kind) via the provenance log.** Re-editing the same property type on the same work counts once for that user; different users editing the same property each count separately. Applies to spatial, temporal, and the new ontology bucket.

- **Filter the contribute page by collection** with `/contribute/?collection=<identifier|id|short_slug>`; collection landing pages link into it.

- **Copy/paste geometries between the geoextent tool and the contribution form.** A new "Copy extents" button on `/geoextent/` saves all extracted geometries to the browser; a "Paste geoextents" button on the work landing page (when contributing spatial extent) pastes them into the map as editable layers ready to submit.

- **Admin-only "Publish all unpublished works" button on collection pages** — bulk-flips every Harvested or Contributed work in the collection to Published in one click. Curators see no button (admins-only). Draft / Testing / Withdrawn works are deliberately left untouched.

- **Mountain Wetlands and OpenAlex-as-source harvesters now auto-create a Collection** for the source on first run (mirroring the OAI-PMH path from issue #192). New collections start unpublished so admins can review name/description before exposing them on `/collections/`.

- **Separate "Unpublished works" map layer** for admins (main map) and collection curators (collection pages). Features split into two togglable layers, *Published works (N)* and *Unpublished works (N)* (dashed, muted). Popups for unpublished features show a status badge.

- **Work-state-change email notifications** — admins and collection curators receive an email when a user contributes spatial or temporal metadata; contributors receive an email when their work is later published. Per-user opt-out toggle on `/usersettings/`.

- **CSV download with WKT geometry column** (closes #206) — published works are exported as CSV at `/download/csv/` alongside GeoJSON and GeoPackage. New `python manage.py regenerate_data_dumps [--format geojson|gpkg|csv] [--dry-run]` command for synchronous on-demand regeneration.

- **Reverse-geocoded placename and country code on work landing pages**, with a tooltip noting Nominatim as the source. Per-point matches with permalinks to OSM are recorded under `Work.provenance.geocoding` for admins.

- **OpenAlex-as-source harvester** (`source_type='openalex'`) — uses OpenAlex itself as the harvest origin, configured with an OpenAlex Source identifier. New `python manage.py compare_agile_giss_routes` command diffs the OpenAlex and Crossref routes side-by-side.

- **Crossref harvester now extracts authors and biblio fields** (`volume`, `issue`, `first_page`, `last_page`).

- **Reverse-geocoded `placename` and `country_code` on the Work API** — both keys always emitted (null when absent).

- **Collection backlinks on work landing pages** — each work landing page lists the collections it belongs to; unpublished collections shown only to admins.

- **Inline curator-editable collection description** — curators (and admins) edit a collection's description directly on `/collections/<identifier>/`. Plain text only.

- **Admin-only "Manage curators" deep-link on the collection page** — one-click jump to the admin change form anchored on the curators field.

### Fixed

- **Admin "Trigger / Schedule / Retry harvesting" actions and `harvest_journals` now dispatch by `source_type`** instead of hardcoding the OAI task, so non-OAI sources (MaRESS, RSS, Crossref, OpenAlex) run the right harvester. `harvest_journals` also reconciles stale Source rows against `SOURCE_CONFIG` (rewrites `source_type`, fills blank soft fields, preserves admin edits).

- **Inline mutation buttons reflect the new state immediately on production.** Authenticated responses on `/collections/`, the work landing page, and `/subscriptions/` are no longer captured by the site-wide cache, so publish/unpublish/contribute/subscribe state flips on reload. Anonymous responses still cached.

- **"View work details" button now appears in single-feature popups for works without a DOI.**

- **Empty-DOI backfill on re-harvest** — works ingested without a DOI have it filled when a later harvest delivers one. `python manage.py backfill_openalex` also recovers DOIs via OpenAlex matches.

- **Collections entry restored to the burger menu**, and **collection-page work cards always link to the OPTIMAP landing page** rather than to an external URL.

### Changed

- **Mountain Wetlands harvester reads DOIs from the API** (now populated upstream) and feeds them to the OpenAlex matcher; title-only fallback retained for records without one.

- **`/collections/` work counts** show only published works to regular users; admins and curators of a collection get a per-status breakdown (zero-count rows hidden).

- **Data-dump regeneration unified into one umbrella task.** GeoJSON, GeoPackage, and CSV are produced from one intermediate. Retention now keeps the newest N timestamp groups rather than raw files.

- **HTML geotagging meta tags + spec-compliant schema.org `Place.geo` on work landing pages** (closes #222) — landing pages emit `geo.position`, `ICBM`, and (when available) `geo.placename` / `geo.region`. `ScholarlyArticle.spatialCoverage.geo` now follows [schema.org/geo](https://schema.org/geo): point geometries become `GeoCoordinates`, others become `GeoShape` with a `box`. Multi-point geometries reduced to the lowest common ancestor in the address hierarchy.

- **In-memory cache for hot anonymous reads** (closes #180, partially #7) — adds a `LocMemCache` alias for static / low-change anonymous pages (privacy, about, accessibility, feeds list, sitemap, robots.txt) and the work landing page. Responses advertise their TTL via `Cache-Control` and `Expires` headers. Authenticated/staff requests always render live.

- **Zotero / reference-manager metadata on work landing pages** (issue #243) — emits `citation_abstract` (full text), `citation_publisher`, `citation_language`, repeated `citation_keywords`, `citation_volume`/`issue`/`firstpage`/`lastpage`, and `citation_pdf_url`. Collection card pages also carry a COinS fallback for multi-item save.

- **Squashed Django migrations into a single `0001_initial`** in preparation for a clean redeployment.

### Added

- **`Work.placename` + `Work.country_code` populated by reverse geocoding** (issue #222). Gated by `OPTIMAP_GEOCODE_WORKS_ON_SAVE` (off by default). New `python manage.py backfill_placenames [--limit N] [--force] [--sleep 1.1] [--dry-run]` command.

- **OAI-PMH harvester auto-creates a Collection per endpoint** (closes #192) — the first harvest of an OAI/OJS/Janeway source creates a `Collection` and links it. New collections start `is_published=False`.

- **`HarvestingEvent.records_updated` counter** — every harvester tracks updated works separately from created ones, surfaced in the admin and the completion email.

- **Per-source dedup and careful-update flag in every harvester** — duplicate detection scoped to `Source`. New `--update` flag on `harvest_journals` (and `update_existing=True`) refreshes existing same-source works in place; user-contributed spatial/temporal metadata, `status`, and `created_by` are preserved.

- **Mountain Wetlands Repository harvester** (issue #192) — manual-only; run via `python manage.py harvest_journals --journal mountain-wetlands`.

- **Collections** (issue #192, foundation) — new `Collection` model groups works under a curated identifier. Routes: `/collections/`, `/collections/<identifier>/`. Curators add/remove works on the work landing page; admins publish/unpublish from either page. Includes a collection sitemap and burger-menu entry.

- **`Source.source_type` choice field** (`oai-pmh` / `ojs` / `janeway` / `rss` / `crossref-prefix` / `mountain-wetlands`) — replaces the implicit OAI-PMH assumption. Default `harvest_interval_minutes` flips to `0` (manual-only).

- **`Source.collection` foreign key** replaces the legacy `collection_name` string field.

- **Structured `Work.provenance` (JSONField)** with a defined schema (`harvest`, `metadata_sources`, `openalex_match`, `events`, `text_log`). User contributions and admin publish/unpublish actions append to `events`. Pre-migration text values preserved under `text_log`.

### Fixed

- **Crossref-prefix harvester no longer auto-publishes harvested works** — new works default to Harvested, not Published.

- **Work landing page hides the empty map when no geometry is available.** Shows a "no geospatial metadata" notice with a link to the contribution page instead.

- **`Source.save()` no longer queues every source to fire immediately, and no longer resets the schedule on unrelated edits.** New schedules use `now() + harvest_interval_minutes`; the schedule row is preserved when the interval is unchanged.

### Changed

- **Harvesting code split into the `works.harvesting` package** — one module per concern (OAI-PMH, RSS, Crossref, Mountain Wetlands, OpenAlex, sessions, helpers). Existing dotted-path imports (`works.tasks.harvest_*`) continue to resolve.

- **Database indices on hot `Work` query paths** (issue #141) — adds `status`, `(-creationDate, -id)`, `publicationDate`, and a partial `(-creationDate, -id) WHERE status='p'`.

- **HTTP `Referrer-Policy` is now `strict-origin-when-cross-origin`** so the OpenStreetMap tile server stops blocking map requests.

- **OAI-PMH harvester hardened against transient and content-type errors** — sessions get retries (3 attempts, exponential back-off), 30 s timeout, and a User-Agent. Non-XML 200 responses are rejected with a body preview.

### Added

- **`reset_harvest_schedules` management command** — rebuilds every `Harvest Source <id>` schedule with a deferred `next_run`, staggered. Flags: `--dry-run`, `--no-stagger`, `--clear-manual`.

- **`harvest_journals --insert-sources`** — bulk-creates `Source` rows for every entry in `SOURCE_CONFIG` without harvesting. `--include-disabled` opts in disabled journals.

- **Admin UI for harvesting management and log exploration** (issue #228) — `Source` is registered at `/admin/works/source/` with three actions ("Trigger harvesting", "Trigger harvesting for all", "Schedule harvesting"). `HarvestingEvent` admin shows record counts, error message, log block, and a "Retry" action; recent events appear inline on each `Source`.

- **Profanity filter on Recognition Board usernames** — submitted display names are checked against [`better-profanity`](https://pypi.org/project/better-profanity/)'s default list.

- **Flash messages are now visible on every page** — queued Django messages render as dismissible Bootstrap alerts in `base.html`.

- **Contributor Recognition Board** (issue #240) — public `/recognition-board/` page ranking users who contributed spatial or temporal metadata. Five explorer-named tiers (Marco Polo → Roald Amundsen) on a logarithmic scale (1 / 10 / 100 / 1000 / 10000). Opt-in display name set under Settings → Recognition Board.

- **Minimal SEO surface on landing pages** (issue #22) — work landing pages emit Open Graph, Twitter Card, schema.org `ScholarlyArticle` JSON-LD, and Google Scholar `citation_*` tags. Homepage emits `WebSite` + `SearchAction`; regional feeds emit `CollectionPage`. `<link rel="canonical">` on every SEO-relevant page.

- **Open Graph preview images for work landing pages** (issue #22) — `/work/<id>/preview.png` renders a 1200×630 PNG of the work's spatial extent on an OSM basemap. Disk-cached, invalidated on save.

- **`.zenodo.json` deposit metadata** (issue #16) — repository-root metadata file so GitHub releases archived to Zenodo are populated with title, description, creators, license, and links.

<!-- REUSE-IgnoreStart -->
- **REUSE / SPDX license headers on all source files** (issue #30) — every first-party file carries SPDX headers; `REUSE.toml` covers fixtures and vendored assets. Run `reuse lint` to verify.
<!-- REUSE-IgnoreEnd -->

- **Janeway harvesting and `janeway_geometadata` plugin support** (issues #15, #18) — OAI-PMH harvesting picks up geometries published by Janeway journals running [`janeway_geometadata`](https://github.com/GeoinformationSystems/janeway_geometadata). Extraction priority: schema.org JSON-LD → `<link rel="alternate" type="application/geo+json">` → `DC.SpatialCoverage` → `DC.box`.

- **ISO 8601 open intervals** in `DC.temporal` and JSON-LD `temporalCoverage` are parsed correctly: `../2024-12-31` produces `(None, "2024-12-31")`. Single instants stored as `(value, value)`.

- **Geoextent API** — REST endpoints exposing the [geoextent library](https://github.com/nuest/geoextent):
  - `/api/v1/geoextent/extract/` — extract from uploaded files (GeoJSON, GeoTIFF, Shapefile, GeoPackage, KML, CSV, etc.)
  - `/api/v1/geoextent/extract-remote/` — extract from remote repositories (Zenodo, PANGAEA, OSF, Figshare, Dryad, GFZ, Dataverse)
  - `/api/v1/geoextent/extract-batch/` — batch processing
  - Response formats: GeoJSON (default), WKT, WKB. Optional placename via Nominatim / GeoNames / Photon.

- **Geoextent web interface** — interactive tool at `/geoextent/` for file upload, remote extraction by DOI/URL, Leaflet preview, parameter customisation, and result download.

- **Feeds sitemap** — `/sitemap-feeds.xml` listing all regional feeds.

- **Wikidata export** — export work metadata to a Wikibase/Wikidata instance, including extreme points and geometric centre. Configured via `WIKIBASE_*` environment variables.

- **Global regions layer** — toggleable map overlay showing continent and ocean boundaries; click for region details and regional feed.

- **Zoom-to-all features control** — quick-fit button on every map.

- **Geocoding/gazetteer search** — search-by-location on map (Nominatim default; GeoNames optional).

- **Works list with pagination** — `/works/list/` browse page with configurable page size.

- **Regional subscription system** — subscribe to email notifications for new works in selected continents/oceans.

- **Temporal extent contribution** — users contribute temporal extent (start/end dates) alongside or instead of spatial extent. Accepts `YYYY`, `YYYY-MM`, `YYYY-MM-DD`.

- **Status workflow documentation** — all 6 publication statuses (Draft, Harvested, Contributed, Published, Testing, Withdrawn) documented in README with transitions and visibility rules.

- **Burger menu navigation** — top-bar hamburger menu with links to all main pages.

- **Human-readable sitemap** — `/pages` lists all pages with descriptions, organised by category.

- **Custom error pages** — styled 404 and 500 pages matching the application design.

- **"View work details" button on map popups** linking to the work landing page.

- **Paginated popup for overlapping features** — Previous/Next navigation when multiple works overlap.

- **Point geometry highlighting** — circle markers show visual feedback when selected (gold/orange).

- **Admin unpublish action** — admins can unpublish a work, changing status from Published to Draft.

- **RSS/Atom feed harvesting support** alongside OAI-PMH.

- **`harvest_journals` management command** — CLI tool for harvesting from real journal sources with progress reporting.

- **Test coverage** — 40+ tests covering temporal contribution, status workflow, RSS harvesting, and live journal harvesting.

### Changed

- **Contribution page pagination** — `/contribute/` paginates with configurable page size (default 50, max 200).

- **Model terminology alignment** — `Publication` renamed to `Work` throughout. API endpoint changes from `/api/v1/publications/` to `/api/v1/works/`, sitemap to `/sitemap-works.xml`, URL pattern to `/work/<id>/`. Django app renamed from `publications/` to `works/`.

- **Work type taxonomy** — `type` field added with 39 work types (Crossref/OpenAlex vocabulary). Set from source's `default_work_type`; overridden by OpenAlex when available.

- **Removed external CDN dependencies** — all JS and CSS libraries served locally.

- **Improved map accessibility** — keyboard navigation and screen reader support.

- **Regional subscription email notifications** — emails group works by region; each section shows region name, type, count, and a link.

- **Unified contribution workflow** — single "Submit contribution" button covers spatial and/or temporal extent in one action.

- **Unified admin control panel** — admin status, publish/unpublish, provenance, and "Edit in Admin" consolidated into one box at the top of the work landing page.

- **Improved text wrapping** — page titles and abstracts wrap on narrow windows.

- **Unified URL structure** — ID-based URLs changed from `/publication/<id>/` to `/work/<id>/`.

- **Consolidated work identifier logic** — new `Work.get_identifier()` returns the DOI when available, falling back to the internal ID.

- **Refactored `views_geometry.py`** — DOI-based functions wrap ID-based functions; ~36 % reduction.

- **Renamed "Locate" to "Contribute"** — URL, page title, and navigation updated.

- **Contribute page layout refactored** — text overflow fixed.

- **Flexible publishing requirements** — harvested works with geometry can be published directly without requiring user contribution.

- **Contribute page login button improved** — informational disabled button: "Please log in to contribute (user menu at top right)".

- **Simplified footer navigation** — Sitemap, About/Contact/Imprint, Privacy, and data license only. Other links moved to burger menu and sitemap.

### Fixed

- **JavaScript scope error** — `drawnItems is not defined` on the contribution form.

- **GeoJSON geometry detection** — map click detection now reads `layer.feature.geometry` directly, with point-in-polygon (ray casting), point-on-line, and point detection.

- **Map popup null location error** — paginated popup no longer crashes; existing popup is closed before setting a new location.

- **Highlight persistence after popup close** — geometries return to default style when popups close.

- **Individual popups during pagination** — individual feature popups no longer open simultaneously with the paginated popup.

- **Close button highlight clearing** — popup close (X) and ESC clear geometry highlights.

- **First-page highlight race condition** in the paginated popup.

- **CircleMarker style properties** — point geometries use `radius` instead of `dashArray` for proper visual feedback.
