# Managing OPTIMAP

This document is the admin / operator handbook for an OPTIMAP instance. It is a counterpart to [README.md](../README.md) (which targets developers and deployers) and [CLAUDE.md](../CLAUDE.md) (which targets coding assistants): everything here assumes you have a running OPTIMAP and are signed in to `/admin/` as a Django superuser.

For a fully working setup, two processes must be live in addition to the web app:

- the database (PostGIS) — required for everything;
- the Django-Q cluster (`python manage.py qcluster`) — required for harvesting, scheduled emails, and data dumps. The admin will accept actions without it, but they will silently sit in the queue.

## Manage harvesting

Sources and harvesting events are managed entirely through the Django admin under `/admin/works/source/` and `/admin/works/harvestingevent/`. As of v0.12.0 (issue #228) the `Source` model is registered with a dedicated admin and `HarvestingEvent` exposes the full per-run log, error message, and record counts.

### Configure a source

At `/admin/works/source/`, the changelist shows each source's name, OA / preprint flags, last harvest time, harvest interval, latest event status (linked to the event), and total event count. Search runs over `name`, `url_field`, `issn_l`, `publisher_name`, and `openalex_id`. Filter by `is_oa`, `is_preprint`, and `default_work_type`.

#### Source field cheatsheet

The change form is grouped into five fieldsets, mirrored below. **Only the three "Identification" fields are mandatory at the model level** — everything else is optional (or has a sensible default). What you actually need to fill depends on `source_type`; see the per-type walkthrough that follows.

| Fieldset | Field | Mandatory? | What it does |
|----------|-------|-----------|---------------|
| **Identification** | `name` | **Yes** | Display name in admin / `/pages` / `/sitemap`. Free-form. |
| | `source_type` | **Yes** (defaulted to `oai-pmh`) | Selects which harvester runs. See `SOURCE_TYPE_TASKS` in [works/models.py](../works/models.py). |
| | `url_field` | **Yes** | Source endpoint URL. **Meaning depends on `source_type`** — see below. |
| **Harvesting configuration** | `harvest_interval_minutes` | Defaulted to `0` | `0` = manual-only. `>0` = auto-schedule via Django-Q (`Harvest Source <id>`). |
| | `collection` | Optional | Default `Collection` for harvested works. Blank is fine — works are simply not auto-added; OAI-PMH/OJS/Janeway also auto-create one if blank. |
| | `default_work_type` | Defaulted to `article` | Default `Work.type` for harvested works (overridden by OpenAlex metadata when present). |
| **OpenAlex / external IDs** | `openalex_id` | **Yes for `source_type=openalex`**, optional otherwise | OpenAlex Source identifier (`S<digits>`, or the full `https://openalex.org/S<id>` URL). The display URL exposed by the public Source API as `openalex_url` is derived from this field on the fly. |
| | `doi_prefix` | **Yes for `crossref-prefix` and `geoscienceworld`**, ignored otherwise | DOI prefix used by Crossref-based harvesters, e.g. `10.1190` (SEG) or `10.5194` (Copernicus). Replaces the old hardcoded Copernicus fallback. |
| | `source_titles` | Optional, `crossref-prefix` only | JSON list of Crossref `container-title` filter values (e.g. `["Scientific Data"]`). Required when `doi_prefix` covers a broad prefix (e.g. `10.1038` = all Springer Nature) to restrict both harvesting and Crossref stats to the target journal. Auto-populated from `SOURCE_CONFIG`. |
| | `crossref_filter` | Optional, `crossref-prefix` only | Raw Crossref filter clauses used as the harvest base query instead of `prefix:<doi_prefix>` (comma-separated, e.g. `member:311,type:posted-content`). Use when a venue spans several DOI prefixes that share a Crossref member/type — e.g. ESS Open Archive (`10.1002/essoar.*` + `10.22541/essoar.*`). Blank harvests by `doi_prefix`. |
| | `doi_contains` | Optional, `crossref-prefix` only | Case-insensitive DOI-substring include-filter applied client-side. Use to narrow a query that spans multiple venues with no separating Crossref field — e.g. `essoar` for ESS Open Archive within the Wiley `posted-content` slice. Blank keeps all query matches. |
| | `issn_l` | Optional | For `crossref-prefix` sources, also drives an `issn:` Crossref filter — use when the journal name contains a comma (Crossref treats commas in `filter=` as clause separators, breaking `container-title:` for such journals). `abbreviated_title` — display only. |
| **Display metadata** | `publisher_name`, `homepage_url`, `is_oa`, `is_preprint`, `tags` | Optional | Display only — none of these affect harvesting. |
| **Statistics (auto-populated)** | `works_count`, `cited_by_count`, `last_harvest`, `statistics` | Read-only | Auto-populated. `statistics` is a JSON field holding `openalex_works_count` / `openalex_fetched_at` (when `openalex_id` is set), `oai_works_count` / `oai_fetched_at` (OAI sources), and `crossref_works_count` / `crossref_fetched_at` (`crossref-prefix` sources). |

> **Auto-scheduling rule:** A `Source` runs on a Django-Q schedule only when *both* `source_type` is a schedulable kind (i.e. listed in `Source.SOURCE_TYPE_TASKS`, which today covers all current types) *and* `harvest_interval_minutes > 0`. Saving the source creates / updates the `Schedule` named `Harvest Source <id>`. Setting the interval back to `0` removes the schedule. The change page also lists the five most recent `HarvestingEvent`s inline.

#### What to enter per `source_type`

For each type, only mandatory and type-specific fields are listed; defaults / display fields are optional everywhere.

##### OAI-PMH (`oai-pmh`, `ojs`, `janeway`)

- **`url_field`** — full ListRecords URL with `verb=ListRecords&metadataPrefix=oai_dc` (and `&set=…` if needed). Example: `https://e-docs.geo-leo.de/server/oai/request?verb=ListRecords&metadataPrefix=oai_dc`.
- That's it. The harvester (`works.tasks.harvest_oai_endpoint`) reads only `url_field` from the Source row. Leaving `collection` blank causes the first successful harvest to auto-create one (slugged from `name`, `is_published=False` until you review it).

##### RSS / Atom feed (`rss`)

- **`url_field`** — feed URL. Example: `https://www.nature.com/sdata.rss`.
- The harvester (`works.tasks.harvest_rss_endpoint`) does a plain GET; no auth, no API key.

##### Crossref by DOI prefix (`crossref-prefix`)

- **`url_field`** — display only. Set it to something representative, e.g. `https://api.crossref.org/works?filter=prefix:10.5194`.
- **`doi_prefix`** — the DOI prefix to filter on (e.g. `10.5194`). Falls back to `10.5194` if blank for backwards compatibility.
- **`source_titles`** — optional JSON list of Crossref `container-title` filter values. Required for broad prefixes (e.g. `["Scientific Data"]` for 10.1038). Auto-populated from `SOURCE_CONFIG`; manual edits are preserved. Also drives the per-harvest Crossref total-works-count stat.
- **`crossref_filter`** — optional raw Crossref filter clauses (comma-separated) used as the base query **instead of** `prefix:<doi_prefix>`. Use it when a venue spans more than one DOI prefix that share a Crossref member/type. The canonical case is **ESS Open Archive** (see its subsection below): `member:311,type:posted-content`.
- **`doi_contains`** — optional case-insensitive DOI-substring include-filter, applied client-side, that narrows the query (prefix or `crossref_filter`) to a single venue. The canonical case is **ESS Open Archive**: `doi_contains=essoar` keeps only `…/essoar.*` records and discards Authorea (`…/au.*`) from the shared Wiley `posted-content` slice. Leave blank to keep all matches. Because the full query slice is walked to find the matching subset, the auto-populated `crossref_works_count` stat reflects the **whole slice**, not the filtered subset.
- **Incremental harvesting** — after the first successful harvest, scheduled runs automatically add a Crossref `from-update-date` clause (watermark = previous completed event's date − 2 days), so only re-indexed records are fetched instead of re-walking the whole slice. The first run (no prior completed event) is a full backfill.
  - To **force a full backfill** on a source that already has completed events (e.g. to recover the whole catalogue after a gap), pass `--full`: `python manage.py harvest_sources --source essoar --full --update`. This ignores the watermark and re-walks the entire slice — no need to delete `HarvestingEvent` rows by hand. Pair it with `--update` so already-known records are reconciled in place rather than skipped.
  - To set an **explicit window**, pass `--since YYYY-MM-DD` (e.g. `--since 2024-01-01`); only records Crossref re-indexed on or after that date are fetched. `--full` and `--since` are mutually exclusive and apply only to `crossref-prefix` sources (ignored by other source types; under `--async` they error rather than silently drop for sources that can't honor them).
- **Deterministic paging** — all `crossref-prefix` harvests page with `sort=indexed` (newest-indexed first). Crossref's default relevance ordering is unstable under deep cursor paging and can silently truncate a long backfill, so it is never used.
- Harvest with `python manage.py harvest_sources --source copernicus [--source-title "<title>"]` to filter to a specific container title.
- **Journals with commas in the title.** Crossref parses commas inside `filter=` as clause separators, so `container-title:Foo, Bar` is silently split into two broken clauses. Use `issn_l` on the Source instead (or `--source-issn <issn>` on the CLI): the harvester emits `filter=prefix:10.5194,issn:2193-0864`, which Crossref handles correctly. The built-in `copernicus-gi` source entry ("Geoscientific Instrumentation, Methods and Data Systems") uses this pattern.

##### GeoScienceWorld (`geoscienceworld`)

Enumerates articles from Crossref by DOI prefix, then fetches geographic coordinates from each article's GSW landing page via geoextent's built-in GSW content provider (uses `curl_cffi` for Cloudflare bypass; parses WKT `<coordinates>` elements from GeoRef metadata).

- **`url_field`** — display only. Set it to the GSW journal homepage, e.g. `https://pubs.geoscienceworld.org/seg`.
- **`doi_prefix`** — **required**. DOI prefix for the journal family, e.g. `10.1190` (SEG), `10.1144` (GSL), `10.1180` (Mineralogical Society).
- Throttle between geoextent calls is controlled by `OPTIMAP_GSW_THROTTLE` (default 2 s).
- Temporal/epoch extraction is not yet implemented — tracked in [#257](https://github.com/GeoinformationSystems/optimap/issues/257) pending [nuest/geoextent#122](https://github.com/nuest/geoextent/issues/122).

| Field | Value (SEG example) |
|-------|---------------------|
| `name` | `GeoScienceWorld — SEG Journals` |
| `source_type` | `geoscienceworld` |
| `url_field` | `https://pubs.geoscienceworld.org/seg` |
| `doi_prefix` | `10.1190` |
| `default_work_type` | `article` |
| `harvest_interval_minutes` | `0` (manual until smoke run passes) |

##### Mountain Wetlands Repository (`mountain-wetlands`)

- **`url_field`** — MaRESS API endpoint. Example: `https://andes.mountain-wetlands-repository.info/api/v1/items/`.
- Bespoke harvester (`works.tasks.harvest_mountain_wetlands`); see [works/harvesting/mountain_wetlands.py](../works/harvesting/mountain_wetlands.py).

##### OpenAlex source (`openalex`)

The harvester (`works.tasks.harvest_openalex_source`) needs the OpenAlex Source identifier `S<digits>`. It looks for an `S<digits>` substring in two fields, in this order — **first match wins**:

1. `openalex_id` — recommended. Set to the bare ID, e.g. `S4210203054`, or the full URL `https://openalex.org/S4210203054`.
2. `url_field` — fallback. Any URL containing the ID works (e.g. `https://api.openalex.org/sources/S4210203054`).

The public Source API exposes a derived `openalex_url` (`https://openalex.org/<S-id>`) computed from `openalex_id`; it is no longer a stored field, so there is no second OpenAlex field to keep in sync.

#### AGILE GI collection — two sources, one collection

The **AGILE GI** collection (`/collections/agile-gi/`) is fed by two `SOURCE_CONFIG` entries that share the same `collection_name`:

| Key | Source name | Publisher | Years | harvest task |
|-----|------------|-----------|-------|-------------|
| `agile-giss` | AGILE: GIScience Series (Crossref) | Copernicus | 2020–present | `harvest_crossref_prefix` |
| `agile-gi-lncs` | AGILE: Springer LNCS Proceedings | Springer | 2008–2019 | `harvest_crossref_book_list` |

Run both with:

```bash
python manage.py harvest_sources --source-prefix agile-gi
```

Or individually:

```bash
python manage.py harvest_sources --source agile-giss
python manage.py harvest_sources --source agile-gi-lncs
```

The Springer source uses `harvest_crossref_book_list` — it iterates over 12 hardcoded ISBNs (one per conference year), calling `filter=prefix:10.1007,isbn:{isbn}` for each, and merges all results into a single `HarvestingEvent`. Springer chapters carry no spatial/temporal metadata from Crossref or from publisher landing pages; geometry can be contributed by users via the contribution workflow at `/contribute/`.

#### ESS Open Archive (`essoar`) — two DOI eras, one Crossref slice

The **ESS Open Archive** collection (`/collections/ess-open-archive/`) harvests AGU's [ESSOAr](https://essopenarchive.org/) preprint server. ESSOAr has no usable native API — its Atypon/Cloudflare platform blocks OAI-PMH, REST, RSS and even its sitemap — so it is harvested via Crossref. Two complications drive the config:

1. **Two DOI eras.** ESSOAr launched in 2018 on its own platform (DOIs `10.1002/essoar.*`) and migrated to Authorea in 2022 (DOIs `10.22541/essoar.*`). No single DOI prefix covers both, and prefix `10.1002` alone is all of Wiley (millions of records).
2. **Indistinguishable from Authorea.** Both eras are registered under Wiley Crossref **member 311**, work type **`posted-content`** — the same as Authorea — and Crossref carries no `container-title`/`group-title` that separates them.

The solution: harvest the Wiley `posted-content` slice (`member:311,type:posted-content`, ~94k records incl. Authorea, which **does** contain both ESSOAr eras) and keep only DOIs containing `essoar`. The `essoar` config therefore sets `crossref_filter="member:311,type:posted-content"` and `doi_contains="essoar"` (see the `crossref-prefix` field notes above). Works are labelled as preprints. Create and harvest with:

```bash
python manage.py harvest_sources --create-sources --source essoar
python manage.py harvest_sources --source essoar --max-records 50   # smoke test
```

A full backfill walks the whole `member:311,type:posted-content` slice (paged deterministically by `sort=indexed`); subsequent scheduled runs are incremental (`from-update-date`). The `crossref_works_count` stat reflects the whole slice (~94k), not the ESSOAr subset.

> **Recovering a stale ESSOAr catalogue.** If the admin count looks capped (e.g. it sits at a few thousand while [essopenarchive.org](https://essopenarchive.org/) reports far more), the cause is almost always that the source is being **re-harvested incrementally**. The default run derives a `from-update-date` watermark from the last completed event, so it only re-fetches *recently re-indexed* records — nearly all already stored — and therefore **never backfills the historical ESSOAr records that predate the first harvest**. `--max-records` does **not** help: it caps the number of *matched* (`essoar`) records, not how far the walk reaches, so raising it changes nothing when the incremental window itself is small. To re-walk the entire catalogue, force a full backfill:
>
> ```bash
> python manage.py harvest_sources --source essoar --full      # no --max-records cap
> ```
>
> Because a full ~94k-record walk over hours is fragile (cursor expiry, transient empty pages), a full backfill of a `crossref_filter` source is automatically **partitioned into yearly deposit-date windows** (`from-created-date`/`until-created-date`, the *stable* deposit date — the index date is refreshed constantly and would pile everything into the latest window). Each window is a bounded walk, so a transient failure costs one window rather than the whole catalogue; any shortfall is reported in the harvest email as a "stopped early" warning. Schedule a periodic `--full` resync so incremental drift can't permanently hide backlog.

> **Version dedup.** ESS Open Archive mints a **separate DOI per version** of a preprint (`…/v2` in the current era, a trailing `.2` in the legacy era), so a raw full backfill would create one Work per version and over-count the catalogue (~50k versioned DOIs vs. ~23k unique preprints). OPTIMAP collapses these automatically onto the **latest version**: `works.utils.doi.normalize_versioned_doi` derives the versionless base, and `works.dedup.reconcile_versions` (on every harvest save) / `works.dedup.version_sweep` (the scheduled `dedup_sweep` and `dedup_works`) keep the highest version live and turn older versions into `status='r'` redirect tombstones — the same machinery as [OpenAlex deduplication](#openalex-deduplication-locations) but keyed on the DOI base instead of an `openalex_id`.

> **Investigated and rejected** as harvest routes for ESSOAr: the ESSOAr platform API (Cloudflare 403 on every endpoint); OpenAIRE by data source (registered as `opendoar____::ada71870…` but **0 products collected** — "Not yet registered"); OpenAIRE/Crossref by publisher (both report `Wiley`); and BASE/CORE (IP-/key-gated, and they harvest the same blocked OAI endpoint). The Wiley member + `posted-content` slice is the only complete, tractable route.

> **Note on the `doi_prefix` field**: the Springer source requires `doi_prefix = "10.1007"` on the `Source` row. `harvest_sources --insert-sources` sets this automatically; if you create the source manually in admin, set it explicitly.

Minimum-viable example for **AGILE GIScience Series (Copernicus via OpenAlex)**:

| Field | Value |
|-------|-------|
| `name` | `AGILE GIScience Series (OpenAlex)` |
| `source_type` | `openalex` |
| `url_field` | `https://api.openalex.org/sources/S4210203054` *(any placeholder works as long as `openalex_id` is set)* |
| `openalex_id` | `S4210203054` |
| `default_work_type` | `proceedings-article` |
| `is_oa` | ✓ *(display flag)* |
| `harvest_interval_minutes` | `0` *(start manual, raise once a smoke run succeeds)* |
| `collection` | optional — pick or create `agile-gi` |
| `publisher_name`, `homepage_url` | optional display fields |

> **Common error:** if you create the source with `source_type=oai-pmh` and the AGILE-GISS OAI URL (`https://oai-pmh.copernicus.org/oai.php?…&set=agile-giss`), the harvester will fail with HTTP 404 — Copernicus's OAI-PMH endpoint has been dark since 2025-12. Switch `source_type` to `crossref-prefix` with `doi_prefix=10.5194` and `source_titles=["AGILE: GIScience Series"]`, or use the `agile-giss` built-in entry.
> **Faster than typing it in:** `python manage.py harvest_sources --insert-sources` creates both AGILE source rows (and every other built-in entry from `SOURCE_CONFIG`) idempotently — see "Bootstrap the admin from the source config" below. Only do the manual admin route when you need a source that's not in `SOURCE_CONFIG`.

> **Source.collection is optional at create time, but always populated by the time the first harvest finishes.** Leaving the collection field blank when you create a Source is **not** an error:
> - **OAI-PMH / OJS / Janeway sources** auto-create a Collection on first harvest, slugged from the source name (e.g. `"Earth System Science Data"` → identifier `earth-system-science-data`). The new Collection starts **unpublished** so it does not show up on `/collections/` until you review the auto-derived name and description and flip `is_published`. Review auto-created collections at `/admin/works/collection/?is_published__exact=0` before publishing.
> - **RSS / Crossref / MaRESS / OpenAlex sources** don't auto-create — they get their Collection from `harvest_sources --insert-sources` instead. If you leave `Source.collection` blank for one of these and run a harvest, the works simply aren't added to any collection, and curators can add them by hand from each work landing page later.
> 
> Either way, when a collection **is** set on the source, every work created during harvest is automatically added to it (additive — pre-existing memberships under other collections are preserved). To put a fresh source's harvest into a specific Collection, create the target Collection first under `/admin/works/collection/` and link it from the Source change page; see "Create a new collection (no harvest needed)" below.

### Trigger or schedule a harvest from the admin

Select one or more sources in the changelist and pick an action from the **Action** dropdown:

| Action | What it does |
| --- | --- |
| **Trigger harvesting for selected sources** | Enqueues an immediate `async_task('works.tasks.harvest_oai_endpoint', source.id, user.id)` per selected source. Returns immediately. |
| **Trigger harvesting for all sources** | Same, but enqueues every `Source` in the database. Useful after a cluster restart or to force a full refresh. |
| **Schedule harvesting for selected sources** | Creates a one-off `Schedule` named `Manual Harvest Source <id>` that runs at the next cluster tick. Skips sources that already have such a schedule (you'll get a warning message). |

All three actions are **non-blocking**: they queue work and return. Progress is observed at `/admin/works/harvestingevent/`.

> **Why async?** Earlier versions ran the harvest synchronously inside the admin request, which routinely tripped gunicorn's worker timeout on non-trivial sources. The new actions hand off to Django-Q immediately. The cluster must be running for them to actually execute.

CLI alternatives still work and are documented in README §[Harvest works from real sources](../README.md#harvest-works-from-real-sources): `python manage.py harvest_sources --source <slug>` (with `--list`, `--all`, `--create-sources`, `--user-email`, `--max-records`).

**Run a CLI harvest in the background:** add `--async` to enqueue each source as a Django-Q task instead of harvesting inline (needs a running `qcluster`). For every source the command prints the **`HarvestingEvent #<id>`** it pre-created plus the Django-Q task id, e.g. `Enqueued works.tasks.harvest_crossref_prefix — HarvestingEvent #427 (Django admin), Django-Q task id: 8673…`. Open `/admin/works/harvestingevent/<id>/` to watch that exact run progress from `pending` → `in_progress` → `completed`/`failed` (the task reuses the pre-created row rather than making a new one). The Django-Q task id is only useful inside `qmonitor` / Django-Q → Tasks; the `HarvestingEvent` id is the handle that maps to the admin row.

**Recover from a thundering-herd schedule state:** `python manage.py reset_harvest_schedules` rebuilds every `Harvest Source <id>` recurring schedule with a properly deferred `next_run` and (by default) staggers them across the smallest harvest interval so the cluster doesn't get hit with every source at once. Use this after a bulk `--insert-sources` run on a deployment that pre-dated the `Source.save()` fix, or any time you find every source firing simultaneously. Flags: `--dry-run` (preview), `--no-stagger` (set every `next_run` to `now + its own interval`), `--clear-manual` (also delete leftover `Manual Harvest Source <id>` one-off rows from the admin "Schedule harvesting" action). Sources with `harvest_interval_minutes = 0` are skipped (manual-only, matching `Source.save()`), so the seeded "User contributions" source and other manual sources are never scheduled.

**Bootstrap the admin from the source config:** `python manage.py harvest_sources --insert-sources` creates one `Source` row per (enabled) entry in `harvest_sources`'s `SOURCE_CONFIG` without harvesting. Each insert also gets-or-creates a `Collection` from the entry's `collection_name`. **New collections start unpublished** — review the name/description and flip the "Is published" toggle in `/admin/works/collection/` when a collection is ready to be public. After running it, every configured source appears at `/admin/works/source/` (linked to its collection) and can be triggered with the actions above. Re-running is idempotent (existing rows by name or URL are left alone) and never changes a collection's publish status, so a re-insert on every deployment won't re-expose collections you have unpublished. Add `--include-disabled` to insert sources whose upstream is currently broken (e.g. the Copernicus OAI-PMH 404). All inserts default to `harvest_interval_minutes = 0` (manual-only) so the cluster is not flooded after a bulk insert; `Source.save()` dispatches to the correct task per source type when you later raise the interval.

### Inspect harvesting events

At `/admin/works/harvestingevent/`, each row is one harvest run. The changelist shows:

- `id`, linked `source`, `status` (`pending` / `in_progress` / `completed` / `failed`),
- `started_at` and a computed `duration` (`Ns` or `Nm Ms`),
- `records_added`, `records_with_spatial`, `records_with_temporal`,
- a truncated `error_message` (full text on the change page).

Filter by `status`, `source`, or the `started_at` date hierarchy. Free-text search runs across `source__name`, `source__url_field`, `error_message`, **and the full `log_text`** — so you can search the logs directly for things like a problematic DOI or a parse-error string.

Open an event to see the full log in a scrollable `<pre>` block. The log is the summary captured by `HarvestWarningCollector` during the run and uses prefix glyphs for severity:

- 🔴 errors (e.g. fatal upstream failures, parse errors that aborted a record),
- 🟡 warnings (e.g. records skipped, individual fields ignored),
- 🔵 notable info (e.g. fallback geometry sources used).

Events are machine-created — manual `add` is disabled in the admin. To re-run a failed source, select one or more events and choose **Retry selected harvesting events**: this re-enqueues `harvest_oai_endpoint` for each event's source via `async_task`. A new `HarvestingEvent` row will appear per source; the original `failed` event is left in place as history.

### Deduplication and updates

Every harvester (OAI-PMH, RSS, Crossref, MaRESS) routes its inserts through a shared helper that looks up an existing Work by DOI or URL **scoped to the harvest's `Source`** before deciding what to do. Four outcomes:

| Pre-existing match | `--update` flag | Outcome |
| --- | --- | --- |
| None | n/a | New Work created. |
| Same `Source` | off (default) | Duplicate skipped silently. |
| Same `Source` | on | Existing Work updated in place — see "Careful update" below. |
| Different `Source` | n/a | Skipped with an info log message (exact same DOI/URL). |

> This DOI/URL exact-match step only catches the *same* identifier harvested twice. **Different versions of one work** (a preprint DOI vs. the published DOI) have different identifiers and are each created — then linked automatically by OpenAlex-id deduplication, below.

### OpenAlex deduplication (locations)

OpenAlex assigns one work id to a scholarly work and lists every hosting copy (journal version, preprint, repository copies) under `locations[]`. OPTIMAP captures these into `Work.locations` (credited to OpenAlex) and uses the shared OpenAlex id to **merge duplicate works automatically — no human review** (`works/dedup.py`):

- The version OpenAlex marks as `primary_location` becomes the **canonical** OPTIMAP work and keeps its DOI/URL. The other versions become `status='r'` **redirect tombstones**: excluded from all listings/feeds/map/API-list, kept only so their identifiers still resolve.
- Every known identifier of the work — each version's DOI, the OpenAlex id, external ids (pmid/pmcid/mag), and OpenAlex location landing URLs — resolves to the canonical work and **302-redirects** to it (landing page and API detail). The landing page lists all copies under "Also available at".
- Merging is **lossless** for spatial/temporal extents: the canonical's is kept; a non-primary's fills an empty one; a genuine conflict is recorded under `provenance.dedup_conflict` for audit. The merge writes `provenance.dedup` on the canonical and `provenance.redirect` on each tombstone (see [Work provenance](#work-provenance)).

**When it runs.** Automatically at harvest time and in the contribute-by-DOI flow (adding a preprint DOI links it to an existing article). For pre-existing data, run the backfill:

```bash
python manage.py dedup_works                 # backfill locations on ALL works w/ an OpenAlex id, then merge duplicates
python manage.py dedup_works --locations-only # only populate locations, no merging
python manage.py dedup_works --source essd --limit 100 --dry-run
python manage.py dedup_works --async          # enqueue works.tasks.dedup_sweep (needs qcluster)
```

The default run **populates `Work.locations` on every record with an OpenAlex id** (re-fetching the OpenAlex payload, rate-limited), not only duplicates. Progress is reported per work and per merged group (to the terminal for the sync command, to the Django-Q worker log for `--async`). Set `OPTIMAP_DEDUP_AUTO_MERGE=False` to capture/expose locations without auto-merging. This is separate from **enrichment** sources (OpenAlex/OpenAIRE) which fill empty fields; see [OpenAIRE enrichment](#openaire-enrichment).

**OpenAlex rate limits & API key.** Direct DOI lookups (`/works/doi:…`) are always free. Title/author fallback searches (used when a DOI is missing or not found) count against a daily budget: ≈ 100 searches anonymous, ≈ 1 000 with a free API key. During large harvest runs or a full `dedup_works` backfill, the anonymous budget can be exhausted, producing silent 429 errors that drop enrichment for affected works — those will have `openalex_id = NULL`. Get a free key at <https://openalex.org/settings/api> and supply it in one of two ways (the DB value takes precedence over the env var):

- **Django admin** (recommended for running instances): **Django admin → Service tokens → add** an *OpenAlex API* row and paste the key into the **Refresh token** field. No restart required.
- **Environment variable / `.env`**: set `OPTIMAP_OPENALEX_API_KEY=<key>` before starting the server.

Works that lost enrichment can be recovered once the key is configured — see [Recovering missing OpenAlex enrichment](#recovering-missing-openalex-enrichment) below.

#### Recovering missing OpenAlex enrichment

Works that lost their OpenAlex match due to 429 errors have `openalex_id = NULL`. Use `backfill_openalex` to re-enrich them without re-downloading from the original source:

```bash
# Re-enrich all works with openalex_id = NULL (default)
python manage.py backfill_openalex

# Preview only — shows what would be matched, writes nothing
python manage.py backfill_openalex --dry-run

# Restrict to one source, process slowly to stay within budget
python manage.py backfill_openalex --source essoar --throttle 1

# Re-enrich even already-matched works (e.g. after a matcher improvement)
python manage.py backfill_openalex --all --limit 500
```

`backfill_openalex` queries OpenAlex by DOI then title/author, applies fill-if-empty enrichment (won't overwrite existing field values), and records attribution in `Work.provenance.metadata_sources`. Redirected works (`status='r'`) are skipped. `--throttle` (default 0.1 s) controls the per-work delay; raise it to `1` or more if you are near your daily search budget.

Alternatively, `python manage.py harvest_sources --source <id> --update` re-fetches **all** metadata from the original source and re-runs enrichment inline — useful when the source itself has been updated, but heavier than a targeted enrichment backfill. The per-work **Re-harvest** button on the work landing page does the same for a single work.

**Un-merge (reverse a merge).** Merges are reversible. In the **Django admin → Works**, filter the list by status **Redirected**, select the wrongly-merged duplicate(s), and run the **"Un-merge (re-promote redirected duplicates)"** action: each tombstone returns to status **Harvested** and is detached from its canonical work's `provenance.dedup`. (Equivalent in a shell: `from works.dedup import unmerge; unmerge(work)`.) To inspect a merged-away duplicate without un-merging it, open it from the Redirected-filtered admin list or pass `?include=redirected` to the API.

**Retire a deprecated Source and move its Works to a replacement:** when a source moves platforms (e.g. EarthArXiv migrating off eScholarship to its own CDL-backed OAI-PMH endpoint) and you end up with an old `Source` row whose Works should really belong to the new one, use `python manage.py migrate_source_works`:

```bash
# Preview only
python manage.py migrate_source_works --from-source "eScholarship Publishing" --to-source EarthArXiv --dry-run

# Reassign all Works (source FK, collection membership, provenance audit event)
python manage.py migrate_source_works --from-source "eScholarship Publishing" --to-source EarthArXiv

# Reassign, then delete the old Source if it's left with zero Works
python manage.py migrate_source_works --from-source "eScholarship Publishing" --to-source EarthArXiv --delete-empty
```

`--from-source`/`--to-source` accept a numeric `Source` id or an exact (case-insensitive) `name`. For each migrated Work the command: re-points `source`, swaps `collections` membership from the old source's default collection to the new one's, detaches `job` if it pointed at one of the old source's `HarvestingEvent`s (required so a later `Source.delete()` cascade can't cascade-delete the Work through `Work.job`), and appends a `source_migration` event to `provenance.events` (see [Work provenance](#work-provenance)). `--delete-empty` only deletes the old `Source` (and its now-orphaned `HarvestingEvent`s) when zero Works remain attached — it never deletes a Source with Works still on it.

#### Careful update (`--update` flag)

`python manage.py harvest_sources --update` (or `update_existing=True` on the task functions) refreshes existing same-source works in place. The update is deliberately conservative:

- **Preserved when the new harvest brings nothing for them:** `geometry`, `timeperiod_startdate`, `timeperiod_enddate`, `abstract`, `keywords`, `authors`. The first three often come from a user contribution through OPTIMAP that the source still does not provide; the latter three may have been filled by an enrichment source (OpenAIRE, OpenAlex) that the harvest origin lacks — we don't want a silent re-harvest to wipe either a curator's work or an enriched abstract.
- **Never overwritten:** `status` (a Published Work stays Published, never flips back to Harvested) and `created_by` (audit trail).
- **Refreshed from the new harvest:** title, topics, OpenAlex enrichment fields, the `provenance.harvest` and `provenance.metadata_sources` and `provenance.openalex_match` sections, and the `Source` FK (and `abstract`/`keywords`/`authors` when the new harvest actually carries them).
- **Audit trail:** a `harvest_update` event is appended to `Work.provenance.events` (existing events including user contributions are preserved).

Use `--update` when you want OpenAlex enrichment to re-run on previously-harvested works (e.g. after a matcher change), or when an upstream metadata change should propagate without losing curator additions. Without it, re-running the harvester is a no-op for already-known records.

#### Re-harvest a single work (landing-page button)

Staff viewing any **DOI-bearing** work's landing page (`/work/<identifier>/`) see a **Re-harvest** button next to Publish/Unpublish. Clicking it (with a confirm prompt) **synchronously** re-fetches that work's metadata from Crossref by DOI and re-runs all enrichment steps (OpenAlex inline + OpenAIRE), then reloads the page. For the bibliographic metadata it uses the same careful-update policy (`_carefully_update_work`), so `status`, `created_by`, and `abstract`/`keywords`/`authors` (when the fresh harvest brings nothing) are preserved, and a `harvest_update` event is appended to `Work.provenance`.

**Geometry and temporal extent are refreshed from the source, not preserved blindly.** Crossref JSON carries no spatial/temporal data, so re-harvest additionally re-fetches the landing-page HTML and re-extracts geometry (`DC.SpatialCoverage` / `geo+json` / JSON-LD) and time period — the same parsers the OAI/prefix harvesters use. It **overrides** the stored value **unless a user contributed it**: if `Work.provenance.events` contains a `contribution` event with `spatial` (resp. `temporal`) in its `kinds`, that value is left untouched (`work_has_contribution_kind`). The outcome per field (`updated` / `preserved (user-contributed)` / `no … found at source`) is reported in the success message, and a `reharvest_source_extents` event is recorded. Overriding geometry bumps `lastUpdate`, so the landing-page cache, map, and country/region joins refresh immediately.

Unlike a normal harvest, re-harvest **bypasses the per-source dedup guard** so it updates the work regardless of which source originally harvested it (the work's existing `Source` is kept; the `HarvestingEvent` is attributed to it). The button is hidden for works without a DOI (Crossref lookup is by DOI). Endpoint: `POST /work/<identifier>/reharvest/` (staff only). Code: `works.harvesting.crossref.reharvest_work`; view `works.views_geometry.reharvest_work`.

### OpenAIRE enrichment

OPTIMAP enriches works from the [OpenAIRE Graph API](https://graph.openaire.eu/docs/apis/graph-api/) as a second enrichment source besides OpenAlex. Its main job is to recover **abstracts** (and, when empty, keywords, authors, journal pagination — `volume`/`issue`/`first_page`/`last_page` — `language`, and `publisher`) for works whose harvest origin does not supply them — most notably the AGILE Springer LNCS chapters (`agile-gi-lncs` source, DOI prefix `10.1007/978-…`), for which Crossref carries no abstract and the publisher landing page is not scraped.

**How it works.** Enrichment is **fill-if-empty**: it only populates a field that is currently empty and never overwrites a value from the owning source or an earlier enrichment (precedence `original_source`/`crossref` > `openalex`/`openaire`). A single work is resolved by DOI via `GET https://api.openaire.eu/graph/v1/researchProducts?pid=<doi>`; the abstract is the longest entry in `results[0].descriptions[]`, with JATS/HTML markup (`<jats:p>…`) stripped to plain text before storing. Pagination comes from `results[0].container` (`vol`/`iss`/`sp`/`ep`), the `language` from `container`-sibling `language.code` (ISO 639-2 alpha-3, e.g. `eng`), and the `publisher` from the `publisher` string. Every decision is written to `Work.provenance`: the per-field origin (`metadata_sources.abstract = "openaire"`, etc.), an `openaire_enrich` event listing `fields_filled` and `fields_offered_not_applied` (values OpenAIRE had but that were *not* applied because a value already existed), and an `openaire_match` block (`status: matched|none`, plus `openaire_id` and a public `url`). On a match the work landing page shows a **"View in OpenAIRE"** link (built from `openaire_match.url`), mirroring the OpenAlex link. See [Work provenance](#work-provenance).

**On every harvest (all sources).** When `OPTIMAP_OPENAIRE_ENRICH_ON_HARVEST=True` (default), each successful harvest enqueues an async Django-Q sweep (`works.harvesting.openaire.enrich_event_from_openaire`) that looks up **every** work in that event with a DOI — not only those missing a field. Works that are missing any enrichable field (abstract, keywords, authors, volume, issue, first_page, last_page, language, publisher) get filled; works that already have everything still get an `openaire_match` record (and, on a match, an `openaire_enrich` event noting the offered-but-not-applied fields), so the OpenAIRE consultation is always auditable. The sweep runs **off** the harvest critical path and throttles between requests. The Django-Q cluster must be running for it to execute. Set `OPTIMAP_OPENAIRE_ENRICH_ON_HARVEST=False` to disable it fleet-wide. (The `enrich_openaire` backfill command below deliberately keeps its missing-field filter — this full audit trail is built going forward, not retroactively.)

> **Bounded chunks (interruptible) & the Django-Q timeout.** Because the sweep and the `enrich_openaire --async` backfill throttle between requests, a full run over many DOIs would take far longer than the global `Q_CLUSTER['timeout']` (600s). Rather than running as one long task (which ignored the cluster stop signal — you couldn't stop it from the console without killing the whole `qcluster`), each task now processes works only until a wall-clock budget `OPTIMAP_OPENAIRE_ENRICH_CHUNK_SECONDS` (default **480s**, below the cluster timeout) elapses, then **re-enqueues a continuation** with an `id`-cursor. Each chunk is therefore short, so the cluster's stop signal is honoured between chunks and no task outlives the cluster timeout. The per-task timeout override `OPTIMAP_OPENAIRE_ENRICH_TASK_TIMEOUT` now defaults to **0** (use the cluster default); set a non-zero value only if you deliberately want long single-task runs.
>
> **Circuit breaker (fail fast + email).** `fetch_openaire_record` distinguishes a *systemic* failure — HTTP 401/403/408/429, any 5xx, or a connection/timeout — from a genuine "this DOI is not in OpenAIRE". After `OPTIMAP_OPENAIRE_ENRICH_ABORT_AFTER` (default **5**) *consecutive* systemic failures, the sweep/backfill **emails active staff** (`openaire_enrich_aborted.en.txt`) and **fails the Django-Q task**, instead of grinding through thousands of doomed requests and spamming identical warnings. A repeated `403 Forbidden` almost always means the OpenAIRE access token/refresh token needs renewing (see [Renewing the OpenAIRE refresh token](#renewing-the-openaire-refresh-token)) or OPTIMAP is being rate-limited/blocked. Live per-work enrichment (Crossref harvest, contribute-by-DOI, re-harvest) stays tolerant — a single systemic blip there is logged and skipped, never raised.

**Backfill existing works** with `enrich_openaire`:

```bash
python manage.py enrich_openaire --collection agile-gi        # AGILE GI works missing a field
python manage.py enrich_openaire --doi-prefix 10.1007/978- --limit 50
python manage.py enrich_openaire --dry-run                     # query OpenAIRE, write nothing
python manage.py enrich_openaire --throttle 1                  # when a token is set (see below)
python manage.py enrich_openaire --collection eartharxiv --async  # run in the background (needs qcluster)
```

Flags: `--collection <identifier>`, `--doi-prefix <prefix>`, `--source <id|name>` (narrow the selection), `--limit N`, `--throttle SECONDS` (default `OPTIMAP_OPENAIRE_ENRICH_THROTTLE`), `--force` (query even works that already have all target fields), `--dry-run`, `--async`.

`--async` enqueues the whole backfill as a single Django-Q task (`works.tasks.enrich_openaire_backfill`) instead of blocking the terminal — useful for large, rate-limited backfills. It prints the enqueued task **id** and its **humanized name** and returns immediately; progress and the processed/updated/no-match/failed summary land in the Q worker log and the task result. A `qcluster` must be running, otherwise the task sits in the broker queue and never executes. The single-task design keeps `--throttle` and the OpenAIRE rate limit honored centrally (unlike one-task-per-work fan-out).

To track it in the Django admin: while it waits it appears under **Django Q → Queued tasks** (identified by the raw `id`); once it runs it moves to **Successful tasks** (or **Failed tasks**), where the searchable *Name* column shows the humanized name printed by the command — Django-Q does not show the raw UUID in those lists, so note the name from the command output.

**Rate limits & token.** OpenAIRE allows **60 requests/hour** anonymously and **7200/hour** with a token. For anything beyond a few dozen works authenticate (see below) and lower the throttle (e.g. `OPTIMAP_OPENAIRE_ENRICH_THROTTLE=1`). The token is sent as a Bearer header; transient `429`/`5xx` responses are retried with backoff. OpenAIRE metadata is **CC-BY** — OPTIMAP credits OpenAIRE as a data source.

#### Renewing the OpenAIRE refresh token

There are two ways to authenticate against OpenAIRE; the first needs no SSH access and is the recommended way to operate a deployment:

1. **Refresh token in the database (admin, no SSH).** OpenAIRE's [authentication flow](https://graph.openaire.eu/docs/apis/authentication/) issues a **refresh token** that is valid for **one month**, which OPTIMAP exchanges for a short-lived (~1 h) access token as needed. The refresh token is stored in the **`ServiceToken`** admin (`/admin/works/servicetoken/`). To set or rotate it:
   1. Open <https://develop.openaire.eu/personal-token> and click **"Get a refresh token"**; copy the value.
   2. In the OPTIMAP admin, open (or add) the **OpenAIRE Graph API** `ServiceToken` row, paste the value into **Refresh token**, and save. Saving stamps the set-time and clears any cached access token.
   3. Optional: select the row and run the **"Refresh access token now"** action to confirm the refresh token works — a success message means OpenAIRE returned an access token.

   Because the refresh token expires monthly, a **weekly** Django-Q task (`works.tasks.check_service_token_renewals`) checks every stored token: if one expires within the next **9 days** (`OPTIMAP_OPENAIRE_RENEWAL_REMINDER_DAYS`) it emails **all active staff** the links and these steps; otherwise it just logs its run and does nothing. Since this is purely a window check (no per-token deduplication), a token may be flagged on one or two consecutive Mondays before it expires. The other relevant settings are `OPTIMAP_OPENAIRE_REFRESH_TOKEN_DAYS` (default 30) and `OPTIMAP_OPENAIRE_ACCESS_TOKEN_TTL` (default 3600).

2. **Static personal access token (env var).** Alternatively set `OPTIMAP_OPENAIRE_TOKEN` to a short-lived personal access token with a TTL of 1 hour. It is used only when no DB refresh token is configured (resolution order: DB access token → `OPTIMAP_OPENAIRE_TOKEN` → anonymous), so it remains a valid fallback for deployments that prefer environment configuration.

The `ServiceToken` table and the reminder machinery are **generic over a list of services** (see `works/utils/service_tokens.py`); OpenAIRE is currently the only registered connector.

### Email notifications on completion / failure

`harvest_oai_endpoint` sends a result email to the user that triggered the run (the user who clicked the action; falls back to silently skipping if there is no user). Subject lines are `✅ Harvesting Completed for <collection>` or `❌ Harvesting Failed for <collection>`. To debug locally, point Django at the console backend in `.env`:

```env
EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
```

### Work-state-change notifications

Separate from harvest emails, OPTIMAP sends notification emails when a user-visible *Work* state change happens. The dispatcher lives in [`works/notifications.py`](../works/notifications.py); the event registry is `WORK_EVENT_HANDLERS`.

| Event | Recipients | Body highlights |
| --- | --- | --- |
| `contribution` | all active `is_staff` users + every active curator of every `Collection` the work is in (minus the contributor) | work title + DOI + link to `/work/<identifier>/`; *transparency block* with **roles + counts** of the other notified parties (`Notified: 1 admin and 2 curators of 'Mountain Wetlands', 'AGILE-GISS'`); heads-up that any of them can publish the work concurrently. |
| `publish` | every distinct active `Contribution.user` for the work (minus the actor doing the publish) | "thank you, your work is now public" + title + DOI + their contribution kinds + the public landing-page URL. |

Both events route through Django-Q (`async_task`) so the request that triggered the state change stays fast. Recipient resolution happens synchronously in the caller's transaction — the queue payload is just a list of user IDs.

**Recipient gating (applies to all admin-routed emails — contribution-to-review, curator-change, and new-user-registration):**

- **Active accounts only.** A deactivated account (`is_active=False`) is never emailed, even if it is staff or a curator.
- **Opt-out honored uniformly.** The `UserProfile.notify_work_events` flag (opt-out, default on) gates all three of these emails — a staff member who turns it off receives none of them. (Earlier, only `contribution` honored it; `curator-change` and the new-user email did not.) An opted-out user is not notified even when they are the actor or the curator being added/removed.

The link in every task-sent email is built from `settings.BASE_URL` (= `OPTIMAP_BASE_URL`), so set that environment variable to the public host on each deployment or the links will point at the development default.

Republish suppression: `notify_work_event(work, "publish", …)` stamps `provenance.publication_notified_at` after the first fan-out and returns early on subsequent calls, so a publish→unpublish→republish cycle does not re-notify contributors.

**To add a new state-change notification** — e.g. notify the original contributors when an admin *unpublishes* their work — write a private `_enqueue_<event>(work, actor)` function (resolves recipients + calls `async_task` on a sibling `send_*` task), add it to `WORK_EVENT_HANDLERS`, and call `notify_work_event(work, "<event>", actor=request.user)` after the relevant `work.save()`. The dispatcher is best-effort: any handler exception is logged but never crashes the state change.

### Where things live in code

For maintainers cross-referencing the admin features above:

- Admin classes & actions: [works/admin.py](../works/admin.py) — `SourceAdmin`, `HarvestingEventAdmin`, `RecentHarvestingEventInline`, `_enqueue_harvest`, `trigger_harvesting_for_specific`, `trigger_harvesting_for_all`, `schedule_harvesting`, `retry_event`.
- Harvesters: [works/harvesting/](../works/harvesting/) — one module per source type (`oai.py`, `rss.py`, `crossref.py`, `mountain_wetlands.py`), with shared helpers in `common.py` (HarvestStats, dedup helpers, `complete_harvest` / `fail_harvest` / `send_harvest_email`), `sessions.py` (HTTP session factories), `metadata_html.py` (geometry + temporal extraction), and `openalex.py`. Persists `records_added`, `records_updated`, `records_with_spatial`, `records_with_temporal`, `log_text`, and `error_message` (truncated to 1000 chars) on the event. The public entry points are re-exported from [works/tasks.py](../works/tasks.py) so Django-Q dotted-path schedules (e.g. `works.tasks.harvest_oai_endpoint`) keep working.
- Models: [works/models.py](../works/models.py) — `Source`, `HarvestingEvent` (`error_message`, `log_text`, `records_added`, `records_with_spatial`, `records_with_temporal`; index on `(source, -started_at)`).
- Migration: [works/migrations/0003_harvestingevent_error_message_and_more.py](../works/migrations/0003_harvestingevent_error_message_and_more.py).
- Tests: [tests/test_admin_harvesting.py](../tests/test_admin_harvesting.py), [tests/test_regular_harvesting.py](../tests/test_regular_harvesting.py).
- OpenAIRE refresh-token workflow: `ServiceToken` model ([works/models.py](../works/models.py)) + admin ([works/admin.py](../works/admin.py) `ServiceTokenAdmin`), token exchange in [works/harvesting/openaire.py](../works/harvesting/openaire.py) (`get_openaire_access_token`) wired through `_openaire_session()` in [works/harvesting/sessions.py](../works/harvesting/sessions.py), the service registry [works/utils/service_tokens.py](../works/utils/service_tokens.py), the weekly reminder `check_service_token_renewals` + `schedule_service_token_renewal_check` ([works/tasks.py](../works/tasks.py), registered in [works/apps.py](../works/apps.py)), template `works/templates/email/service_token_renewal.en.txt`, and [tests/test_service_tokens.py](../tests/test_service_tokens.py).

### User contributions by DOI

Logged-in users can add a publication to OPTIMAP from the `/contribute/` page by submitting its DOI (the collapsible **"Add a work by DOI"** form). The DOI is validated client-side, then `POST /api/v1/works/contribute-doi/` either redirects the user to the existing work (if already present, case-insensitive DOI match) or harvests the single DOI from Crossref and runs OpenAlex + OpenAIRE enrichment **synchronously** before redirecting to the new work's contribution page.

- **Where they land:** every user-submitted DOI becomes a `Work` with status **Harvested (`h`)** attached to a dedicated **"User contributions"** `Source` (and its auto-created collection), seeded by migration `0026` and fetched-or-created at runtime by `works.harvesting.crossref.get_user_contributions_source`. Filter the admin Works list by this source to review them.
- **Provenance & recognition:** a `doi_contribution` event is appended to `Work.provenance` (see [Work provenance](#work-provenance)), and a `Contribution` row with the new `doi` kind is created so the submission counts on the [Recognition Board](#manage-the-recognition-board) and in the `contributed_dois` statistic on `/statistics/`.
- **Rate limit & quota:** the endpoint is per-user rate-limited via `OPTIMAP_CONTRIBUTE_DOI_RATE` (default `30/hour`). The synchronous OpenAIRE call consumes the OpenAIRE quota — anonymous is 60 req/hour deployment-wide, raised to 7200/hour when an OpenAIRE `ServiceToken` is stored (see [OpenAIRE enrichment](#openaire-enrichment)).
- **Code:** `harvest_crossref_doi` in [works/harvesting/crossref.py](../works/harvesting/crossref.py); `WorkViewSet.contribute_doi` + `ContributeDoiThrottle` in [works/viewsets.py](../works/viewsets.py); `normalize_doi` in [works/utils/identifiers.py](../works/utils/identifiers.py); UI in [works/templates/contribute.html](../works/templates/contribute.html) + `static/js/doi-validate.js` / `static/js/contribute-doi.js`; tests in [tests/test_contribute_doi.py](../tests/test_contribute_doi.py).

## Manage collections

A **`Collection`** groups works under a curated identifier — typically a journal (`scientific-data`, `eartharxiv`), a thematic dataset (`mountain-wetlands`), or a community-curated series (`agile-gi`). A `Work` can belong to **multiple** collections (`Work.collections`, M2M). Each `Source` has an optional default `collection`, so works harvested from that source can be tagged automatically.

### Public surfaces

- `/collections/` — index of all *published* collections (anonymous-visible). Staff users see unpublished collections too, with inline publish/unpublish buttons.
- `/collections/<identifier>/` — detail page for one collection: map of works, work cards, links to the external homepage if set.
- `/<short_slug>/` — optional vanity URL that 301-redirects to the canonical detail page. Useful for short, citable URLs (e.g. `/agile-gi`). Set `short_slug` in admin to opt in.
- Both `/collections/` and each published `/collections/<identifier>/` page are listed in `sitemap.xml` (machine) and `/pages/` (human-readable).

### Create a new collection (no harvest needed)

A collection does **not** need a harvested source. A curator can build one entirely by hand from existing works.

1. Go to `/admin/works/collection/add/`.
2. Fill in `name` — `identifier` is auto-suggested from the name (URL-safe slug); edit it if you want a different URL. The detail page will live at `/collections/<identifier>/`.
3. Optional fields:
   - `short_slug` — vanity URL: `/<short_slug>/` 301-redirects to the canonical detail page (e.g. `agile-gi` for the AGILE conference series). Pick something distinctive enough not to collide with future top-level routes.
   - `description` — Markdown-friendly text shown on the detail page.
   - `homepage_url` — external homepage shown as a link in the detail-page header.
4. Add `curators` (see "Promote a user to curator" below) and check `is_published` when you want it visible to anonymous users and listed in sitemaps. Leave `is_published` unchecked while staging — staff users will still see it under `/collections/`, marked as **Unpublished**.
5. Save.

To **populate the collection**, sign in as a curator (or staff user) and visit any `/work/<DOI>/` landing page: an "Add to **{Collection}**" button appears for every collection you curate, and a "Remove from **{Collection}**" button replaces it once the work is in. Memberships are independent — a single work can be added to several collections, and removing it from one leaves the others intact. For bulk assignment, use the `Work` admin's `collections` filter horizontal widget on individual works, or call `work.collections.add(collection)` from `python manage.py shell` for scripted bulk additions.

### Promote a user to curator

A curator is just a user listed in a `Collection.curators` (M2M to `CustomUser`). There is no separate role or permission to grant — staff status is **not** required.

Two ways to add curators:

1. **From the collection's change page (recommended for one-off):** open `/admin/works/collection/<id>/change/`, scroll to the **Curators** widget (a Django `filter_horizontal` two-pane selector), pick the user(s) on the left, click the right-arrow to move them into the chosen list, and Save. Search by username/email in the widget's filter box.
2. **From the user's perspective (recommended when granting one user access to many collections):** there is no admin-side mirror widget on `CustomUser` today, so use the shell:
   ```bash
   python manage.py shell -c "
   from django.contrib.auth import get_user_model
   from works.models import Collection
   user = get_user_model().objects.get(email='curator@example.com')
   for slug in ['mountain-wetlands', 'agile-gi']:
       Collection.objects.get(identifier=slug).curators.add(user)
   "
   ```

Once added, the user immediately sees curator buttons on `/work/<DOI>/` landing pages for those collections — no logout/login required. To revoke, deselect the user in the same widget (or `collection.curators.remove(user)` in the shell).

If a user does not yet exist (e.g. you want to invite an external collaborator to curate `agile-gi`), have them sign in once via the magic-link flow at `/loginconfirm/` to create their `CustomUser` row, then add them as a curator. Curators do not need `is_staff = True`; granting it would also give them access to the full Django admin, which is usually overkill for this role.

### Curate a collection

At `/admin/works/collection/`, the changelist shows each collection's name, identifier, short slug, publication state, and counts (works, curators, sources). Open a collection to:

- edit `name`, `description`, `homepage_url`,
- toggle `is_published` (only published collections are visible to anonymous users and listed in sitemaps),
- assign `curators` — a many-to-many to `CustomUser`. Curators get **Add to {X}** / **Remove from {X}** buttons on every work landing page in the OPTIMAP UI, scoped to the collections they curate.
- set an optional `short_slug` for the vanity redirect.

Bulk actions on the changelist: **Publish selected collections** and **Unpublish selected collections**.

### Inline admin controls in the public UI

Staff users see admin chrome integrated into the public pages:

- on `/collections/` — a status badge per row (Published / Unpublished) plus a one-click Publish/Unpublish button and a deep-link to the admin change page;
- on `/collections/<identifier>/` — an **admin** banner (staff only) with the collection-wide Publish/Unpublish, Edit logo, and Edit in Admin controls;
- on every work landing page — for users who curate at least one collection, an "Add to / Remove from" button per applicable collection.

These mirror the per-work admin controls on the work landing page (Publish / Unpublish / Edit in Admin), keeping the workflow consistent for both admins and curators.

#### Curation box on the collection page

Curators **and** admins of a collection see a **Curation** card on `/collections/<identifier>/` with two parts:

- **Review & publish** — bulk-publish the collection's unpublished works (status Harvested `h` or Contributed `c`; Draft/Testing/Withdrawn are left untouched). Two buttons: **Publish all N unpublished works** and **Publish N with extent** (only those that already have a spatial or temporal extent). Both POST to `/collections/<id>/publish-works/` (`extent_only=1` for the latter) and are now allowed for **curators**, not just staff — only publishing/unpublishing the *whole collection* stays admin-only (in the admin banner). A **"Show N works ready to publish"** filter link (`?filter=publishable-extent`) narrows the work list below to exactly the "Publish N with extent" set so a curator can eyeball them before publishing; "show all works" clears it. The collection's total work count in the admin banner is unaffected by the filter.
- **Curators** — add a curator by email or remove an existing one (the same membership managed by the admin `filter_horizontal` widget).

### Unpublished works on the maps

The main map (`/`) and the per-collection map (`/collections/<identifier>/`) split publication features into two Leaflet overlays in the layer-control panel (top-right):

- **Published works (`N`)** — solid teal outline, full opacity. Visible to everyone; `N` is the count of published features in the current view.
- **Unpublished works (`N`)** — same hue, dashed outline, ~50 % opacity. Only registered for users who can see non-`Published` works: site admins on the main map, plus curators of the collection on collection pages. Both overlays are on by default; toggle the Unpublished overlay off to declutter when triaging.

Popups for unpublished features carry an inline status badge (Draft / Harvested / Contributed / Testing / Withdrawn) and a *"not visible to anonymous users"* caveat, mirroring the per-row badges in the collection card list. Anonymous users still see only `status='p'` features from the API/view, so the Unpublished overlay never appears for them.

### Source types

The `Source.source_type` choice field selects the harvester pipeline:

| value | dispatched task | typical usage |
|---|---|---|
| `oai-pmh` | `harvest_oai_endpoint` | Generic OAI-PMH endpoint, unknown platform |
| `ojs` | `harvest_oai_endpoint` | OJS journal (typically with the [geoMetadata OJS plugin](https://github.com/TIBHannover/geoMetadata)) |
| `janeway` | `harvest_oai_endpoint` | Janeway journal (typically with the [geometadata Janeway plugin](https://github.com/GeoinformationSystems/janeway_geometadata/)) |
| `rss` | `harvest_rss_endpoint` | RSS / Atom feed |
| `crossref-prefix` | `harvest_crossref_prefix` | Crossref `works` API filtered by DOI prefix (`doi_prefix` field) |
| `mountain-wetlands` | `harvest_mountain_wetlands` | Bespoke harvester for the Mountain Wetlands Repository (MaRESS) |
| `openalex` | `harvest_openalex_source` | OpenAlex `works` API filtered by `primary_location.source.id` |
| `geoscienceworld` | `harvest_geoscienceworld` | Crossref enumeration + geoextent coordinate extraction from GSW landing pages |

`oai-pmh`, `ojs`, and `janeway` share the same harvester today; the distinction captures the platform so the metadata extractor's priority order (schema.org JSON-LD → `geo+json` link → `DC.SpatialCoverage` → `DC.box`) and admin UI can branch in future without another migration.

#### OpenAlex-as-source — `openalex` source type

OpenAlex is most useful in OPTIMAP as an *enrichment* layer (DOI-based matching during harvest), but for journals where the upstream OAI-PMH endpoint is unreliable and the Crossref payload is bibliographic-only (e.g. Copernicus journals, where the OAI-PMH endpoint at `oai-pmh.copernicus.org/oai.php` has been HTTP 404 since 2025-12), OpenAlex is also the most complete data source available. The `openalex` source type makes that an explicit, schedulable harvest path.

- **Identifier:** the harvester pulls `https://api.openalex.org/works?filter=primary_location.source.id:<S-id>` where `<S-id>` is taken (in order) from `Source.openalex_id` or `Source.url_field` — anything containing the `S<digits>` token works. Set the bare ID (e.g. `S4210203054` for AGILE GIScience Series) on the Source change page; the public Source API derives the `openalex_url` (`https://openalex.org/<S-id>`) from it on the fly.
- **Pagination:** cursor-based (`cursor=*`), 200 records per page, polite-pool User-Agent. Honors `--max-records` and accepts a `sort` kwarg (`publication_date:desc` is the default for the comparison command, unset for production runs).
- **What you get from OpenAlex:** title, abstract (reconstructed from `abstract_inverted_index`), publication date, authors, keywords, AI-derived topics, biblio (volume / issue / pages), `openalex_id`, `openalex_ids` (DOI / PMID / etc.), `openalex_open_access_status`, `openalex_fulltext_origin`, `openalex_is_retracted`, work type.
- **What you don't get:** OpenAlex carries no spatial or temporal coverage. The harvester deliberately does **not** fetch publisher landing pages — for AGILE-GISS we verified that the Copernicus landing pages also carry no `DC.SpatialCoverage` / `DC.box` / schema.org `spatialCoverage` / `geo+json` link, so the round-trip would be wasted work. If you point the harvester at a journal whose landing pages *do* carry spatial metadata, leave a follow-up issue: a per-source toggle for landing-page extraction is the obvious extension.
- **Run it manually:** trigger from the Django admin Source change page, or use `python manage.py harvest_sources --source <identifier> --create-sources` once you have set up a Source row with `source_type=openalex` and the correct `openalex_id`.

#### Mountain Wetlands Repository (MaRESS) — `mountain-wetlands` source type

The MaRESS harvester is bespoke because the API is Zotero-shaped, not OAI-PMH/RSS/Crossref:

- **Run it manually:** `python manage.py harvest_sources --source mountain-wetlands` (also available as a one-click admin action on the Source). Auto-scheduling is intentionally off — `harvest_interval_minutes` defaults to 0 for this source type and the issue (#192) requires the harvest to be manual.
- **Geometry:** built from each item's `study_sites[].location.{latitude, longitude}`. One Point per site, wrapped in a `GeometryCollection`. Records without sites get an empty geometry.
- **Dates:** the API's `date` field is free-text and often year-only (e.g. `"1993"`). The harvester parses the four-digit prefix and stores Jan 1 of that year; both `timeperiod_startdate` and `_enddate` are set to the year string.
- **DOI / OpenAlex enrichment:** the MaRESS API now populates `DOI` for most records. The harvester persists the API DOI directly (normalising `https://doi.org/…` → bare `10.x/y` via `_mwr_clean_doi`). When both a DOI *and* authors come from the API, OpenAlex is **skipped entirely** — no extra metadata to recover and the call wastes rate-limit budget. For records that still lack a DOI or authors, `build_openalex_fields(title, doi=<api_doi_or_None>, author=<first author surname>)` is called as a fallback. Results land in `Work.provenance.openalex_match.status`:
  - `skipped` — API supplied DOI + authors; OpenAlex not contacted,
  - `verified` — strong DOI or title+author match; DOI extracted from `openalex_ids` and saved on the Work,
  - `candidate` — only partial matches; top hits stored in `Work.openalex_match_info` for curator follow-up,
  - `none` — no plausible match; the Work is still saved with the API metadata.
- **Idempotency:** the harvester uses each item's stable API URL (`<source.url_field>/<item-uuid>`) as `Work.url`. Re-running on the same payload is a no-op.
- **Provenance:** `Work.provenance.harvest.original_record` stashes the verbatim API record so curators can re-run enrichment without re-fetching upstream.

### Where things live in code

- Model: [works/models.py](../works/models.py) — `Collection`, plus `Work.collections` (M2M) and `Source.{source_type, collection}`.
- Views: [works/views_collections.py](../works/views_collections.py) — index, detail, vanity redirect, publish/unpublish, add/remove work mutations.
- Templates: [works/templates/collections.html](../works/templates/collections.html), [works/templates/collection_page.html](../works/templates/collection_page.html), and the curator-button block in [works/templates/work_landing_page.html](../works/templates/work_landing_page.html).
- Provenance helper: [works/utils/provenance.py](../works/utils/provenance.py) — `append_event(work, type, **fields)` for contribution / publish / unpublish events.
- Provenance template tag: [works/templatetags/optimap_extras.py](../works/templatetags/optimap_extras.py) — `render_provenance` renders `Work.provenance` JSON readably for admins/curators.
- Sitemap: [optimap/sitemaps.py](../optimap/sitemaps.py) — `CollectionsSitemap`.
- Migration: [works/migrations/0004_collections.py](../works/migrations/0004_collections.py) (`atomic = False`).
- Tests: [tests/test_collections.py](../tests/test_collections.py).

## Work provenance

Every `Work` carries a structured `provenance` JSON field that records where it came from, how its metadata was assembled, and what happened to it over time. The schema is documented in [works/utils/provenance.py](../works/utils/provenance.py).

### Provenance API endpoint

```
GET /api/v1/works/<id>/provenance/
```

Returns the work's provenance record as JSON. No authentication required. The response varies by caller:

| Caller | Response |
|--------|----------|
| Anonymous / regular authenticated user | Public subset (see below) |
| Staff (`is_staff=True`) | Full provenance |
| Curator of any collection this work belongs to | Full provenance |

**Public subset** — keys stripped from the response for non-privileged callers:

- `harvest.original_record` — raw upstream harvest payload (can be large; internal)
- `openalex_match.top_candidate` — verbose raw OpenAlex API response
- `events[*].user_id` — personal data

**Event quick-reference** — `contribution` events carry these optional fields in addition to `kinds`, `at`, and the privileged identity keys:

| Field | Type | When present |
|-------|------|-------------|
| `game` | `true` | Contribution was submitted via the georeferencing game (`/contribute/next/` flow) |
| `geometry_source` | object | NER/geoextent provenance hint passed from the frontend |

**HTTP caching:**

- Anonymous responses: `Cache-Control: public, max-age=3600` (1 hour).
- Authenticated responses: `Cache-Control: private, no-store`.

### Provenance on the work landing page

On every work landing page (`/work/<id>/`), a collapsible **"Show source information"** button appears below the source/collection line. It is visible to all users (anonymous, logged-in, curator). Clicking it fetches `/api/v1/works/<id>/provenance/` once and renders the result inline — the full page does not reload and the provenance payload is not embedded in the initial HTML response.

Staff users additionally see the full provenance (including `original_record`, Wikidata export history, and admin controls) inside the status banner at the top of the page, rendered server-side.

### Provenance schema quick reference

```jsonc
{
  "harvest": {
    "harvester": "harvest_oai_endpoint",   // function name
    "source_name": "Earth System Science Data",
    "source_type": "oai-pmh",
    "source_url": "https://essd.copernicus.org/oai/",
    "harvested_at": "2026-04-30T12:00:00+00:00",
    "harvesting_event_id": 42,
    "doi": "10.5194/essd-16-1",
    "original_record": { ... }             // staff/curators only
  },
  "metadata_sources": {                    // per-field attribution
    "authors": "openalex",
    "author_orcids": "openalex",           // openalex | openaire — source that wrote Author M2M links
    "abstract": "openaire",                // crossref | openaire
    "publisher": "openaire",               // volume/issue/first_page/last_page/language/publisher: openalex | openaire
    "geometry": "DC.SpatialCoverage",      // … | reharvest_html (admin re-harvest)
    "timeperiod": "reharvest_html"         // present when re-harvest overrode temporal extent
  },
  "openalex_match": {
    "status": "verified",                  // verified | unverified | none | skipped
    "score": 0.95,
    "matched_id": "https://openalex.org/W123",
    "top_candidate": { ... }               // staff/curators only
  },
  "openaire_match": {
    "status": "matched",                   // matched | none (recorded for every DOI-bearing work checked)
    "openaire_id": "doi_dedup___::…",      // present when matched
    "url": "https://explore.openaire.eu/search/result?id=doi_dedup___::…",  // present when matched
    "num_found": 1
  },
  "geocoding": {
    "gazetteer": "nominatim",
    "placename": "Sulawesi, Indonesia",
    "n_geocoded": 3,
    "geocoded_at": "2026-04-30T12:00:05+00:00",
    "matches": [ ... ]                     // per-point Nominatim results
  },
  "countries": {                           // how the Work.countries M2M was joined (#261)
    "source": "natural_earth",             // | "manual" for a staff curation decision
    "method": "buffer_snap",               // "intersects" for a direct hit;
                                           // "curator_assigned" / "curator_excluded" when source == "manual"
    "snap_tolerance_degrees": 0.12,        // only when method == buffer_snap (~12 nm Territorial Sea)
    "iso_codes": ["ID"],                   // [] for curator_excluded ("will not be matched")
    "assigned_at": "2026-04-30T12:00:06+00:00",
    "decided_by": 1,                       // manual only: staff user id
    "decided_at": "2026-04-30T12:00:06+00:00" // manual only
  },
  "regions": {                             // how the Work.regions M2M was joined (continents + oceans)
    "source": "global_regions",
    "method": "intersects",                // always a direct hit (no buffer-snap for regions)
    "regions": [                           // multi-valued: a coastal work links continent + ocean
      {"name": "Asia", "region_type": "Continent"}
    ],
    "assigned_at": "2026-04-30T12:00:07+00:00"
    // staff /regions curation instead writes: "source": "manual",
    // "method": "curator_assigned" | "curator_excluded" (regions [] for excluded),
    // "decided_by": <user_id>, "decided_at": "..."
  },
  "dedup": {                               // on the CANONICAL work (absorbed duplicates)
    "openalex_id": "https://openalex.org/W123", // null for doi_version dedup
    "merged_work_ids": [17, 88],
    "merged_identifiers": ["10.31223/preprint", "https://eartharxiv.org/preprint"],
    "method": "openalex_id",               // | doi_version (ESSOAr per-version DOIs)
    "primary_basis": "openalex_primary_location", // | version_rank | existing | doi_version
    "at": "..."
    // optional sibling key "dedup_conflict": [ { "work_id": 88, "kind": "geometry", "at": "..." } ]
  },
  "redirect": {                            // on a REDIRECTED tombstone (work status='r')
    "canonical_work_id": 42,
    "canonical_identifier": "10.5194/essd-16-1",
    "openalex_id": "https://openalex.org/W123",
    "at": "..."
  },
  "events": [                              // chronological audit log
    { "type": "harvest",      "at": "..." },
    { "type": "reharvest_source_extents", "at": "...", "geometry": "updated", "temporal": "updated" }, // admin re-harvest overrode source-derived extents (skipped for user-contributed values)
    { "type": "doi_contribution", "at": "...", "user_id": 42, "doi": "10.5194/..." },  // user added this work by submitting its DOI on /contribute/
    { "type": "contribution", "at": "...", "user_id": 42, "kind": "spatial" },
    { "type": "publish",      "at": "...", "user_id": 1 },
    { "type": "source_migration", "at": "...", "from_source": "eScholarship", "to_source": "EarthArXiv" },
    { "type": "dedup_merge",  "at": "...", "merged_work_ids": [17, 88] },  // on canonical; on a tombstone: { "canonical_work_id": 42 }
    { "type": "dedup_unmerge", "at": "..." },                              // a tombstone was re-promoted
    { "type": "openaire_enrich", "at": "...", "openaire_id": "doi_dedup___::…",
      "doi": "10.1007/978-3-540-78946-8_4",
      "source_url": "https://api.openaire.eu/graph/v1/researchProducts?pid=10.1007/978-3-540-78946-8_4",
      "fields_filled": ["abstract", "first_page", "publisher"], // were empty, now populated (any of abstract/keywords/authors/volume/issue/first_page/last_page/language/publisher)
      "fields_offered_not_applied": ["authors"] // OpenAIRE had a value but one already existed (kept)
    },
    { "type": "country_curation", "at": "...", "user_id": 1, "decision": "assigned", "iso_codes": ["DE", "PL"] }, // staff /countries curation (multi-valued); decision "excluded" for "will not be matched"
    { "type": "region_curation", "at": "...", "user_id": 1, "decision": "assigned", "region": "Asia", "regions": ["Asia", "Indian Ocean"] }, // staff /regions curation; "region" set only for the single-region path; decision "excluded" for "will not be matched"
    { "type": "author_link", "at": "...", "source": "openalex", "linked": 3, "orcids": ["0000-0002-1825-0097"] }, // Author ORCID iDs were linked to the Work.author_links M2M
    { "type": "geometry_repair", "at": "...", "method": "make_valid" }, // an invalid stored geometry was repaired with GEOS make_valid (ST_MakeValid) on save
  ],
}
```

All keys are optional; fresh works start with `{}`.

## Reference-manager / Zotero compatibility

Work landing pages (`/work/<id>/` and `/work/<doi>/`) and collection detail pages (`/collections/<id>/`) emit the metadata that the [Zotero browser connector](https://www.zotero.org/download/connectors) and other reference managers (Mendeley, ReadCube, Citation Web Linker, etc.) read. No setup required — when a reader visits a work landing page with the connector installed, the connector recognises it as a journal article and offers "Save to Zotero". On a published collection page it offers "Save to Zotero (multiple items)" so a curator's curated set can be imported in one click.

What populates in the reader's reference manager (when the OPTIMAP record has the data): title, authors, publication date, DOI, journal title, ISSN, abstract, keywords, language, publisher, volume, issue, page range, and a PDF URL when the harvested URL ends in `.pdf`. Volume / issue / page range, language, and publisher are populated by the harvesters that capture them (Crossref, GeoScienceWorld), the OpenAlex matcher, and OpenAIRE enrichment (fill-if-empty); works whose origin omits them and that never matched OpenAlex or OpenAIRE will be missing those fields. When `Work.language`/`Work.publisher` are empty, the citation language falls back to `en` and the publisher to the harvesting source name. The mechanics are Highwire Press `citation_*` meta tags + `ScholarlyArticle` JSON-LD + a COinS span fallback, all built in [works/seo.py](../works/seo.py) and rendered from [works/templates/work_landing_page.html](../works/templates/work_landing_page.html) and [works/templates/collection_page.html](../works/templates/collection_page.html).

### Geotagging meta tags

Work landing pages also emit the conventional [HTML *Geotagging* meta tags](https://en.wikipedia.org/wiki/Geotagging#HTML_pages) so map-aware crawlers and indexers can discover the work's geographic coverage without parsing the JSON-LD payload:

- `geo.position` — `"lat;lon"` of the geometry's bounding-box centroid.
- `ICBM` — `"lat, lon"`, the [Yahoo variant](https://en.wikipedia.org/wiki/ICBM_address#Modern_use). Both tags are emitted when geometry is present.
- `geo.placename` — the human-readable Nominatim hierarchy (e.g. *"Sulawesi, Indonesia"*), only when `Work.placename` is set.
- `geo.region` — ISO 3166-1 alpha-2 country code(s) from the `Work.countries` M2M (comma-joined for transboundary works), only when the work is linked to at least one country.

The corresponding [schema.org `Place.geo`](https://schema.org/geo) payload follows the spec: single-point geometries are emitted as `GeoCoordinates`, anything else as `GeoShape` with `box="south west north east"` (matching the format already used for region feed pages).

## Reverse geocoding (placename) and country association

OPTIMAP derives two distinct things from a work's geometry:

- **`Work.placename`** — a human-readable Nominatim hierarchy string (e.g. *"Sulawesi, Indonesia"*), via reverse geocoding.
- **`Work.countries`** — a many-to-many link to the `Country` table (ISO 3166-1 alpha-2), via an **offline point-in-polygon join** against the Natural Earth outlines. This powers the `/at/<country>/` pages and the `by_country` statistics. It is **multi-valued**: a transboundary study links every country its geometry intersects.

### Placename (Nominatim)

`Work.placename` is populated by reverse-geocoding the geometry via [Nominatim](https://nominatim.openstreetmap.org/). For multi-point geometries — e.g. the Mountain Wetlands harvester emits one `Point` per study site — every representative point is geocoded separately and the result is reduced to the **lowest common ancestor** in the Nominatim address hierarchy. So a work with sites in Berlin and Munich resolves to *"Germany"* (state diverges, country shared); a work spanning Germany and France resolves to `None` rather than the misleading geometric centroid in northern France. Single-Polygon works contribute one interior representative point. The walk is capped at 20 points so a 500-vertex polygon doesn't trigger 500 Nominatim requests.

Reverse geocoding is **on by default in production** so the `geo.placename` / `geo.region` HTML meta tags are emitted and `Work.provenance.geocoding` is populated by every harvester — set `OPTIMAP_GEOCODE_WORKS_ON_SAVE=False` in the deployment environment to opt out (e.g. for a bulk import where you would rather backfill separately afterwards). The setting is forced off under the test runner regardless, so the suite stays offline. With the flag on, every `Work.save()` (creation or geometry edit) calls `works.services.geocoding.geocode_geometry(geom)` from a `pre_save` signal — each per-point lookup is cached in the per-process `LocMemCache` (key `reverse_geocode:<lat>:<lon>` with 3-decimal-place quantisation, ~100 m, 30-day TTL) so popular regions hit memory after the first lookup, keeping sustained Nominatim traffic well below the 1 req/s courtesy limit. On a complete geocoding outage (no point returned an address) the existing placename is preserved; on a real "geometry spans incompatible regions" outcome it is honestly cleared.

Backfill the placename for an existing deployment:

```bash
python manage.py backfill_placenames                  # all works missing placename
python manage.py backfill_placenames --limit 200      # batch the first 200
python manage.py backfill_placenames --dry-run        # preview only, no DB writes
python manage.py backfill_placenames --force          # re-fetch existing entries too
python manage.py backfill_placenames --sleep 1.5      # increase courtesy delay
```

The command sleeps `--sleep` seconds (default 1.1) between cache *misses* to honour Nominatim's [1 req/s usage policy](https://operations.osmfoundation.org/policies/nominatim/). Cache hits are free. Failures (network errors, no result) are logged at WARNING level and leave the existing placename untouched. Customise the User-Agent string via `OPTIMAP_GEOCODER_USER_AGENT`.

### Country association (offline, issue #261)

`Work.countries` is set by `works.services.countries.lookup_countries`, which intersects the geometry against the simplified `Country` outlines — so **`python manage.py load_countries` must have populated the `Country` table** for this to do anything. The input geometry is repaired with PostGIS `ST_MakeValid` first (so an invalid, self-intersecting geometry can't crash the join with `TopologyException`). When a strict intersection finds nothing, the geometry is buffered by **0.12°** (≈ 12 nautical miles, the Territorial Sea zone) and retried, so coastal / small-island works that fall just outside the simplified outline still resolve to their country. How each work was joined is recorded in `Work.provenance.countries` (`method`: `intersects` or `buffer_snap`, with `snap_tolerance_degrees` for the latter) — see [Work provenance](#work-provenance). It runs in two places, both gated by `OPTIMAP_GEOCODE_WORKS_ON_SAVE`:

- a `post_save` signal (`works.signals.assign_work_countries`) on every save, and
- a **weekly self-healing sweep** (`works.tasks.backfill_work_countries`, scheduled automatically) that links any work which has geometry but no countries yet — covering works saved with the flag off, harvested before #261, or whose geometry only matched after `load_countries` ran. Ocean / no-match works are simply retried each run (the join is cheap). The sweep emails active staff a summary **only when something changed or errored**.

Backfill or re-check on demand:

```bash
python manage.py backfill_work_countries              # link all works missing countries
python manage.py backfill_work_countries --limit 200  # batch the first 200
python manage.py backfill_work_countries --dry-run    # report counts, no DB writes or email
```

#### Staff curation of unmatched works (on `/countries`)

Some works have a geometry but never match a country (open ocean, Antarctica,
geometry just offshore, bad geometry). Staff see a **collapsible "Curation:
works without a country" section on `/countries`** to resolve them:

- A **paginated list** of works with geometry but no country (the same set the
  weekly sweep processes).
- Per work: **assign one or more countries** with an autosuggest chip widget
  (type to search, click/Enter to add a chip, × to remove — the same UX as the
  BoK topic tagger; multi-valued for transboundary studies), or mark it **"will
  not be matched to a country"**.
- A **"Run country backfill now"** button that enqueues
  `works.tasks.backfill_work_countries` (needs a running `qcluster`) and shows
  the task id.

"Will not be matched" assigns a **reserved sentinel country** (`iso_code="ZZ"`,
"No country / not applicable", empty geometry — created by migration
`0032_country_sentinel`) via the normal `Work.countries` M2M. Because the work
then "has a country", the backfill's `countries__isnull=True` query skips it
with no extra logic. The sentinel is hidden from every public country listing
(the `/countries` overview, `/at/`, the `/api/v1/countries/` map layer,
statistics, and `Work.country_codes`).

Both actions record a manual decision in `Work.provenance.countries`
(`source: "manual"`, `method: "curator_assigned"` or `"curator_excluded"`) plus
a `country_curation` event in `provenance.events`. **A manual decision is
preserved across unrelated saves but voided when the work's geometry changes** —
then the work re-runs automated matching and, if still unmatched, returns to the
curation list. To undo an exclusion outside this UI, remove the `ZZ` country
from the work in the Django admin.

You can also **edit a work's countries and regions directly in the Django admin**
on the work change page (`/admin/works/work/<id>/change/`) via the `countries`
and `regions` dual-list (`filter_horizontal`) widgets, next to `collections`.
Admin edits write the M2M directly and do **not** record a `provenance` manual
block, so the self-healing sweeps leave admin-assigned works alone (they only
process works with *no* country/region); clearing all values lets the next sweep
re-populate them.

### Global-region association (offline)

`Work.regions` mirrors `Work.countries` for **continents and oceans**: it is set
by `works.services.regions.lookup_regions`, which intersects the geometry against
the `GlobalRegion` outlines — so **`python manage.py load_global_regions` must
have populated the `GlobalRegion` table** for this to do anything. As with the
country join, the geometry is repaired with PostGIS `ST_MakeValid` first so an
invalid geometry can't crash it with `TopologyException`. There is **no
buffer-snap**: continents and oceans tile the whole globe, so a coastal point
already falls inside an ocean region, and snapping would risk pulling a work into
two adjacent continents. The join is **multi-valued** (a coastal work links its
continent *and* its ocean) and recorded in `Work.provenance.regions` (`method`
`intersects`, or `curator_assigned`/`curator_excluded` for a manual staff
decision — see [staff curation](#staff-curation-of-unmatched-works-on-regions)
below) — see [Work provenance](#work-provenance). It powers the
region feed pages, the regional subscription emails, and the `by_continent` /
`by_ocean` statistics, all of which now read this M2M instead of re-intersecting
geometry on every request. It runs in two places, both gated by
`OPTIMAP_GEOCODE_WORKS_ON_SAVE`:

- a `post_save` signal (`works.signals.assign_work_regions`) on every geometry
  change, and
- a **weekly self-healing sweep** (`works.tasks.backfill_work_regions`, scheduled
  automatically, staggered after the country sweep) that links any work which has
  geometry but no regions yet. The sweep emails active staff a summary **only when
  something changed or errored**.

Backfill or re-check on demand:

```bash
python manage.py backfill_work_regions               # link all works missing regions
python manage.py backfill_work_regions --limit 200   # batch the first 200
python manage.py backfill_work_regions --dry-run     # report counts, no DB writes or email
```

#### Staff curation of unmatched works (on `/regions`)

Mirroring the country curation, staff see a **collapsible "Curation: works
without a region" section on `/regions`** for works that have a geometry but
match no continent or ocean — almost always a coastal point falling in the
sliver gap between the continent and ocean outlines:

- A **paginated list** of works with geometry but no region (the same set the
  weekly sweep processes, minus already-curated works).
- Per work: **assign one or more regions** with the autosuggest chip widget (add
  a work's continent *and* ocean in one pass), or mark it **"will not be
  matched"**.
- A **"Run region backfill now"** button that enqueues
  `works.tasks.backfill_work_regions` (needs a running `qcluster`).

Unlike countries there is **no sentinel region**: both actions write a manual
decision to `Work.provenance.regions` (`source: "manual"`, `method:
"curator_assigned"` or `"curator_excluded"`) plus a `region_curation` event. The
unmatched-list query and the backfill sweep both `exclude` works with a
`provenance.regions.source == "manual"` block, so a curated work — including one
excluded with zero regions — stays resolved. **A manual decision is preserved
across unrelated saves but voided when the work's geometry changes**, after which
the work re-runs automated matching. To undo a decision outside this UI, clear
the work's `provenance.regions` block in the Django admin.

## Author ORCID enrichment

ORCIDs are captured automatically when works are enriched:

- **OpenAlex** — every harvested work already triggers OpenAlex enrichment (inline in harvesters, or via the post-harvest sweep). The `authorships` array in the OpenAlex response now includes `author.orcid` and `author.id`; these are saved to the `Author` model and linked via the `WorkAuthor` through-table.
- **OpenAIRE** — the `enrich_openaire` sweep parses the `pid` list on each author record for ORCIDs (scheme `"orcid"`).

Both paths use **fill-if-empty**: if a work already has `Work.author_links`, a second enrichment source does not overwrite them (pass `force=True` on the backfill command to rebuild).

### Backfill existing works

```bash
python manage.py backfill_author_orcids --limit 500 --dry-run   # preview
python manage.py backfill_author_orcids --limit 500              # OpenAlex + OpenAIRE supplement (default)
python manage.py backfill_author_orcids --source openalex        # OpenAlex only
python manage.py backfill_author_orcids --source openaire        # OpenAIRE only
python manage.py backfill_author_orcids --force                  # rebuild even if links exist
python manage.py backfill_author_orcids --async                  # background (needs qcluster)
```

`--source` accepts `both` (default; OpenAlex first, then an OpenAIRE supplement pass that adds any ORCID not yet linked without overwriting), `openalex` (re-fetches `authorships` from the OpenAlex works API by `openalex_id`), or `openaire` (re-uses the stored OpenAIRE record via `fetch_openaire_record`). Only works that have the matching external id are processed. `--throttle` (default `OPTIMAP_ORCID_BACKFILL_THROTTLE`, 0.1 s) controls the per-work delay.

`--async` enqueues the backfill as a single Django-Q task (`works.tasks.backfill_author_orcids_task`) instead of running it in-process. Like the [OpenAIRE backfill](#openaire-enrichment), it runs in **bounded chunks**: each task processes works until a wall-clock budget (`OPTIMAP_ORCID_BACKFILL_CHUNK_SECONDS`, default 8 min, below the cluster timeout) elapses, then re-enqueues a continuation with an id-cursor — so the cluster stop signal is honoured between chunks. After `OPTIMAP_ORCID_BACKFILL_ABORT_AFTER` (default 5) **consecutive** systemic upstream failures (HTTP 401/403/408/429, any 5xx, or a connection/timeout — "OpenAlex/OpenAIRE is refusing us") the run emails active staff and fails the task instead of grinding through thousands of doomed requests. Requires a running `qcluster`; the command prints the task id and its humanized admin name.

Every link written is recorded in `Work.provenance.metadata_sources["author_orcids"]` and an `author_link` event (see [Work provenance](#work-provenance)).

### Authors API

`GET /api/v1/authors/` — paginated list of ORCID-bearing authors with ≥1 published work.
`GET /api/v1/authors/<orcid>/` — retrieve a single author by bare ORCID iD (e.g. `0000-0002-1825-0097`).
`GET /api/v1/works/?author_orcid=<orcid>` — filter works by author ORCID.

Each work serialization now includes `author_orcids`: a list of `{name, orcid, url}` entries in author-rank order (only ORCID-bearing entries have `orcid` and `url`; entries without an ORCID have `null` for both).

### People pages

`/by/<orcid>/` — published works by this author.
`/by/` — paginated index of all ORCID-bearing authors with published works.
`/browse/` — links to `/by/` under the new "People" section.

The ORCID iD icon (`works/static/img/orcid-id-icon-16.png`) links to `https://orcid.org/<orcid>` (new tab); the author name links to their `/by/<orcid>/` page. Authors without an ORCID are rendered as plain text with no links.

## Block emails and domains (anti-spam)

OPTIMAP can block specific email addresses and entire domains from registering or attempting to log in.

**What it does:**

- Blocks specific emails and entire domains from registering.
- Prevents login attempts from blocked users.
- Lets an admin delete users and instantly block their email and/or domain in a single action.

**Where to manage it:**

- `/admin/works/blockedemail/` — individual addresses.
- `/admin/works/blockeddomain/` — whole domains.

**How to use it:**

1. **Manually add a blocked email or domain.** Go to `/admin/works/blockedemail/` (or `blockeddomain/`) and add a new entry.
2. **Block users via an admin action.** Go to `/admin/auth/user/`, select the offending users, and pick **"Delete user and block email"** or **"Delete user and block email and domain"** from the **Action** dropdown. The user row is deleted and the corresponding blocklist entry is created in the same action.

The blocklist is consulted at signup and at magic-link login time; blocked entries are rejected before any email is sent.

## Manage base map layers

OPTIMAP's Leaflet maps show a layer switcher that lets visitors choose a background tile layer.
The available layers are managed in the Django admin under **Works → Base map layers**.

### Enable or disable a layer

1. Go to **Admin → Works → Base map layers**.
2. Toggle **Enabled** for any row in the inline list (the `list_editable` table).
3. Save — the change is cached for up to 5 minutes and then live on all pages.

### Set the default layer

Check **Is default** on exactly one enabled row.  The admin enforces this:
saving a row as default automatically un-sets the flag on any other enabled row.
If no enabled row has the flag, `createBaseMap` falls back to the first enabled row.

### Supply an API key

Some providers (e.g. `Stadia.*`, `MapTiler.*`, `HERE.*`) require an API key.

1. Add a row (or edit the existing disabled row) for the provider.
2. Put `{"apiKey": "your-key-here"}` (or `{"key": "…"}` — check the provider's leaflet-providers docs) in the **Options** field.
3. Enable the row.

**Important:** enabling a tile provider whose tile URLs are served by a third-party
always requires a matching privacy-policy paragraph.  The four default providers
(OpenStreetMap, CARTO, Esri, OpenTopoMap) are already covered in `privacy.html`.
For any new provider, add a paragraph in the same file before enabling the layer.

### Available provider keys

Provider keys follow the [leaflet-providers.js](https://leaflet-extras.github.io/leaflet-providers/preview/) convention: `Namespace.Variant` (e.g. `Stadia.AlidadeSmooth`, `Esri.WorldTopoMap`).
The vendored file is at `works/static/js/leaflet-providers.js`; pin the version comment at the top when you update it.

### Reorder layers

Set the **Order** field (lower numbers appear first in the switcher) and save.

## Operate the Django-Q cluster

OPTIMAP uses [Django-Q2](https://django-q2.readthedocs.io/) to schedule and run background work — harvesting, monthly subscription emails, data-dump regeneration (GeoJSON + GeoPackage + CSV), and the one-off retry / trigger actions in the harvesting admin. **The cluster must be running for any of those to actually execute.** The admin will accept actions while the cluster is down, but the queued tasks will sit in `django_q_task` until a worker picks them up.

**Run the cluster:**

```bash
python manage.py qcluster
```

In Docker the cluster is started by `etc/manage-and-run.sh`; in a manual deployment, run it under `systemd` (or `supervisord`) so it restarts on failure.

**Monitor:**

The Django-Q [monitor docs](https://django-q2.readthedocs.io/en/master/monitor.html) cover this in depth. The two commands worth knowing:

```bash
python manage.py qmonitor   # live dashboard of cluster activity
python manage.py qinfo      # one-shot stats: cluster status, queue depth, last successes/failures
```

**Inspect and prune schedules and tasks** under `/admin/django_q/`:

- **Scheduled tasks** (`/admin/django_q/schedule/`) — every recurring schedule, including the `Harvest Source <id>` rows created by `Source.save()` and the `Manual Harvest Source <id>` one-offs created by the admin "Schedule harvesting" action. Stale or duplicate rows can be deleted here directly.
- **Successful** / **Failed** tasks (`/admin/django_q/success/`, `/failure/`) — completed task history with full stack traces on failure. Useful for diagnosing harvests that died before their `HarvestingEvent.error_message` could be persisted.

**Catch-up behaviour after downtime:**

The cluster runs with `catch_up: False` (in `Q_CLUSTER`; override with `OPTIMAP_SCHEDULER_CATCH_UP=True`). When the cluster has been down or blocked, Django-Q's **default** is to replay *every* missed interval/cron slot on restart — so a recurring task could execute many times back-to-back. With catch-up off, each recurring schedule instead advances to its next future run and fires **once**. Manual work is unaffected: ad-hoc `async_task` actions (Trigger harvesting, Schedule data dump regeneration now, Calculate statistics now, retries), the `Manual Harvest Source <id>` `ONCE` schedule, and `manage.py harvest_sources` never go through the missed-slot replay path.

The catch-up is **logged**. Recurring schedules pass `intended_date_kwarg="scheduled_for"`, and a run that starts more than `OPTIMAP_SCHEDULED_TASK_CATCHUP_THRESHOLD_MINUTES` (default 5) after its intended time logs a WARNING (`works.utils.scheduling`) noting that intervening missed runs were skipped. Keep the threshold below the smallest recurring interval to avoid spurious notices. The `django-q` logger is configured in `LOGGING`, so the scheduler's own "created task … from schedule" lines are also visible. Note: catch-up only governs the *scheduler's* missed-slot replay — if the scheduler keeps running while only the worker is saturated, on-time enqueues can still back up in the broker queue (drain via `qinfo` or truncate `django_q_ormq`).

**Common failure modes:**

- **Stale dotted paths.** Pre-v0.12.0 schedules referenced `publications.tasks.*` instead of `works.tasks.*`. The cluster fails them with `ImportError`. The monthly/weekly email-digest helpers now register under `works.tasks.*`, but **long-lived deployments may still carry orphaned `publications.tasks.*` rows** that nothing recreates. Clear them **once** (idempotent — safe to skip if there are none):

  ```bash
  python manage.py shell -c "from django_q.models import Schedule; print(Schedule.objects.filter(func__startswith='publications.tasks.').delete())"
  ```

  For other stale paths, delete them from `/admin/django_q/schedule/` and re-create by saving the corresponding `Source` (or run `python manage.py reset_harvest_schedules`).
- **Thundering herd after `harvest_sources --insert-sources`.** Pre-fix `Source.save()` created Schedule rows with `next_run = now`. Recover with `python manage.py reset_harvest_schedules` (see "Manage harvesting" → "Recover from a thundering-herd schedule state").
- **Cluster down, queue grows.** Restart `qcluster` and watch `qinfo` — the queue drains in roughly the order tasks were enqueued. With `catch_up: False` the scheduler no longer adds a burst of missed recurring runs on restart. To skip any remaining backlog, truncate `django_q_ormq` from the dbshell or via the `/admin/django_q/` views.

---

## Suggested further sections

The following sections are **suggested, not yet written**. They cover the rest of the admin surface and are worth filling in as the corresponding features stabilise. Each entry lists what the section should cover and the relevant code/admin URLs so an author can pick one up without further investigation.

### Manage works

`/admin/works/work/` — the core `Work` model.

- The publication status workflow (`d` draft / `p` public) and the `make_public` / `make_draft` admin actions; cross-link to README §[Publication Status Workflow](../README.md#publication-status-workflow).
- Bulk import / export through `django-import-export` (`WorkAdmin` extends `ImportExportModelAdmin`).
- Editing geometry on the Leaflet map widget (`LeafletGeoAdmin`); WKT input via <https://wktmap.com/>.
- The "Email permalinks preview to me" action.
- How harvested vs. manually created works are distinguished (`job` foreing key to `HarvestingEvent`).

### Manage users and access

`/admin/works/customuser/`, `/admin/works/userprofile/`.

- The passwordless magic-link login (10-minute token expiry); the user-facing flow; how to manually grant superuser/staff in the admin.
- `createsuperuser` (CLI) — see README §[Create Superusers/Admin](../README.md#create-superusersadmin).
- `UserProfile` (extended attributes), `EmailLog` (sent-mail audit trail).
- Why login must use `localhost` not `127.0.0.1` during development (CSRF cookie domain).

### Manage subscriptions and notifications

`/admin/works/subscription/`.

- Spatial + temporal filter fields on `Subscription`; how the subscription monthly email is composed.
- The `schedule_subscription_email_task` and `schedule_monthly_email_task` Django-Q schedules (and the `Send Monthly Manuscript Email` admin action).
- How to test the monthly email locally with the console email backend.

### Manage the Recognition Board

`/admin/works/contribution/` (and the public `/recognition/` page).

- Adding / curating contributions, moderating display names.
- The `better-profanity` filter on usernames (CHANGELOG entry under v0.12.0); how to override a false positive manually.
- How the auto-generated `coolname` defaults are filtered before being suggested.

### Manage Wikidata export

`/admin/works/wikidataexportlog/`.

- What gets exported, on what cadence, and how to trigger an export.
- Reading the export log on the change page (mirrors the harvesting-event log pattern).

### Manage data dumps and caches

#### Data dump cache

Cached files in `/tmp/optimap_cache/`; retention controlled by `OPTIMAP_DATA_DUMP_RETENTION` (default: 3 *cycles* — each cycle writes `.geojson`, `.geojson.gz`, `.gpkg`, and `.csv` for the same timestamp).

- The umbrella `regenerate_all_data_dumps` task runs every `DATA_DUMP_INTERVAL_HOURS` hours (default 6, see `optimap/settings.py`). It serialises published works to GeoJSON once and converts the same intermediate to GeoPackage and CSV via the GDAL Python bindings (`osgeo.gdal`, no `ogr2ogr` CLI binary required). The schedule is created on `post_migrate` (`works.apps.schedule_data_dump`); legacy single-format schedules are removed automatically.
- Force a regenerate from a shell:
  ```bash
  python manage.py regenerate_data_dumps                  # all three formats (umbrella)
  python manage.py regenerate_data_dumps --format csv     # only CSV (also: geojson | gpkg)
  python manage.py regenerate_data_dumps --dry-run        # report without writing
  ```
  Runs synchronously in-process — does not need the Q cluster, useful in deploy scripts and for ad-hoc debugging. The same operation is also available via Django-Q (`async_task('works.tasks.regenerate_all_data_dumps')`) and via the admin **Works → action "Regenerate all data exports now"**.
- Staff users can also trigger a regeneration straight from the public **Data & API page** (`/data`): an "Admin view" section there exposes a **"Schedule one-time generation of data dumps now"** button that enqueues the same `regenerate_all_data_dumps` Django-Q task (requires the Q cluster to be running). The refreshed dumps appear on the page once the worker finishes.
- Public download endpoints: `/download/geojson/` (gzipped variant served when the client sends `Accept-Encoding: gzip`), `/download/geopackage/`, `/download/csv/` (CSV with a `WKT` column carrying each work's geometry in OGC Simple Features WKT — useful for `pandas.read_csv` + `shapely.wkt.loads` pipelines).

#### Django caches (`memory`, `default`)

OPTIMAP runs two cache backends — see `optimap/settings.py` (`CACHES =`):

| Alias | Backend | Persists across restarts? | Used for |
|-------|---------|---------------------------|----------|
| `memory` | `LocMemCache` (per Gunicorn worker) | No | `@cache_page` on static-ish views (about / privacy / accessibility / feeds_list / sitemap / robots.txt), the work-landing context cache (24 h, keyed on `work.lastUpdate`), and the per-coordinate reverse-geocode cache (30 day TTL). |
| `default` | `DatabaseCache` (table `cache`) | **Yes** | Login-magic tokens, email-change confirmations, GeoRSS feed bodies. |

**Clearing caches.** Use the `clear_caches` management command (Django itself ships no `clearcache` — see [SO #5942759](https://stackoverflow.com/questions/5942759/best-place-to-clear-cache-when-restarting-django-server)):

```bash
python manage.py clear_caches                    # all configured caches
python manage.py clear_caches --cache memory     # one cache (repeatable)
python manage.py clear_caches --exclude default  # all except default
python manage.py clear_caches --dry-run          # preview, no writes
```

When to clear which:

- **After a deploy that changes templates / context-builders / cached pages** — `clear_caches` (or just `--cache memory`, since the Gunicorn restart already wipes per-process state on its own; the explicit clear is belt-and-braces and safe to run).
- **When users report stale page content but a hard refresh fixes it** — that's a browser-cache problem, not a server one. `Cache-Control: max-age` is set on cached responses; the only server-side fix is to wait it out or change URLs (see "Static files / browser cache" below).
- **When you need to invalidate a specific work's cached landing page without waiting 24 h** — bump the work (any `Work.save()` updates `lastUpdate`, which is part of the cache key, so the next request misses), or clear the `memory` cache.
- **When cleaning up a stuck token state during testing** — `--cache default` (note: this also drops cached GeoRSS feed bodies, which auto-regenerate on the next hit).
- **Routine deploys that should not invalidate active login-magic / email-confirmation tokens** — `clear_caches --exclude default`. The deployment update script ([`docs/deployment-plain.md`](deployment-plain.md)) clears all caches by default; switch to `--exclude default` if mid-flow tokens matter for your operator base.

**Static files / browser cache.** nginx serves `/static/` with `expires 30 d` + `Cache-Control: public, immutable`, and `collectstatic` writes new content at the **same URL**. So even after a server-side clear, browsers can serve a stale CSS/JS bundle for up to 30 days. Hard refresh (Ctrl+Shift+R / Cmd+Shift+R) bypasses this on a single page; the proper fix is filename-hashing via Django's `ManifestStaticFilesStorage` (not currently enabled).

### Manage global regions and predefined feeds

- `python manage.py load_global_regions` — required once after initial setup; loads continent and ocean polygons into `GlobalRegion`.
  - On first run, auto-downloads continents (Esri World Continents) and oceans (MarineRegions Global Oceans and Seas v1, ~128 MB GPKG) into the cache directory.
  - If the simplified ocean GeoJSON is missing, calls `simplify_ocean_geometries` automatically — Shapely tolerance simplify + percentile-based small-hole removal — producing a ~4.7 MB file that gets loaded into `GlobalRegion`.
  - Tunables (env vars, see `.env.example`): `OPTIMAP_OCEAN_SIMPLIFICATION_TOLERANCE` (default `0.05`), `OPTIMAP_OCEAN_SIMPLIFICATION_PERCENTILE` (default `80.0`), `OPTIMAP_GLOBAL_REGIONS_DATA_DIR` (default: command dir; set to a non-volatile path like `/var/opt/optimap/data` for deployment).
  - To refresh: delete the relevant cached file(s) in the data dir (`goas_v01.gpkg`, `goas_v01_simplified.geojson`, `world_continents.geojson`) and re-run the command.
- `python manage.py simplify_ocean_geometries --tolerance <float> --percentile <float>` — re-run the simplification pass standalone (e.g. when retuning); writes `goas_v01_simplified.geojson` from `goas_v01.gpkg` in the data dir.
- How global feeds (`/feeds/georss/<slug>/`) resolve a slug to a `GlobalRegion`.

### Manage countries (for `/at/<country>/` and the countries map layer)

- `python manage.py load_countries` — required once (and after a country data refresh); loads simplified country outlines into the `Country` model from Natural Earth 50m Admin-0 Countries.
  - **Data source & workflow** (mirrors `load_global_regions`: retrieve → simplify → store):
    1. *Retrieve* the public-domain **Natural Earth 1:50m Admin-0 Countries** GeoJSON (the 50m resolution — not the coarse 110m — so borders align well with the basemap). The official `naciscdn.org` host 403s without a User-Agent, so the command uses the canonical **`nvkelso/natural-earth-vector`** GitHub mirror. The file is cached and **committed** at `works/management/commands/ne_50m_admin_0_countries.geojson` (~3.1 MB), so installs/deploys don't re-download; `--force` deletes the cache and re-downloads.
    2. *Read* per feature: `NAME_EN` → `name`, a clean alpha-2 from `ISO_A2` (falling back to `ISO_A2_EH` when `ISO_A2` is non-standard, e.g. Taiwan's `CN-TW`; rows with no clean alpha-2 are skipped), and `CONTINENT` → `continent` (used to group the `/countries/` and `/at/` overviews). When several features share one alpha-2 (e.g. Australia plus its `Australian Indian Ocean Territories` / `Ashmore and Cartier Islands` dependencies, all `AU`), their geometries are **merged** into one record and the name/continent come from the largest-area feature (the sovereign country).
    3. *Simplify* each geometry with Douglas–Peucker (`OPTIMAP_COUNTRY_SIMPLIFICATION_TOLERANCE`, default `0.01` ≈ 1 km; raise to shrink the payload, `--tolerance 0` keeps full resolution) and wrap as `MultiPolygon`.
    4. *Store* via `update_or_create(iso_code=…)`, so re-running updates in place. Data dir honors `OPTIMAP_GLOBAL_REGIONS_DATA_DIR`. Loads ~237 countries/territories.
  - **File sizes:** 50m source GeoJSON ≈ 3.1 MB; the `/api/v1/countries/` payload served to the map is ≈ 1.6 MB at the default 0.01° tolerance (for comparison, the previous 110m source simplified at 0.05° was only ≈ 0.25 MB but far too coarse). The Countries overlay is off by default and fetched lazily only when toggled, so the larger payload has no cost until a user opts in.
  - `iso_code` is ISO 3166-1 alpha-2 and links to works via the `Work.countries` M2M — that's how `/at/<country>/` finds works.
  - Exposed at `/api/v1/countries/` (GeoJSON) and rendered as the toggleable **Countries** overlay (off by default) on the main and collection maps.
  - **Coverage caveat:** `/at/<country>/` undercounts when works are not yet linked to a country — see the [country association](#country-association-offline-issue-261) section. Run `python manage.py backfill_work_countries` (after `load_countries`) to link existing works; the weekly sweep does this automatically.

### Faceted permalink pages and source landing pages (#29, #253)

Short, indexable URLs that list published works:

- `/at/<place>/` — continent/ocean (`GlobalRegion`) or country (`Country`, by the `Work.countries` M2M). Country pages show a **"Feeds & downloads"** card with links to the per-country feeds and downloads listed below.
- `/during/<year>/` — works whose **temporal coverage** (`Work.timeperiod_*` data years) covers the year — *not* `publicationDate`.
- `/on/<topic>/` — works tagged with an OpenAlex topic (`Work.topics`).
- `/in/<source-slug>/` — the **source landing page**: work list + coverage panel (latest `SourceCoverageSnapshot`: coverage %, with-geometry/temporal/open-access rates, contributors, per-year chart) + known totals (`Source.statistics`) + per-source GeoRSS/Atom feeds (`/api/v1/feeds/source-<slug>.rss|.atom`).
- `/browse/` — directory of all of the above with counts (also linked from the footer and `/pages/`).

**Per-country feeds (issue #52):**

| URL | Description |
|-----|-------------|
| `GET /api/v1/feeds/country-<slug>.rss` | GeoRSS feed of published works in the country. |
| `GET /api/v1/feeds/country-<slug>.atom` | Atom feed of the same. |
| `GET /api/v1/countries/<slug>/download/geojson/` | GeoJSON download of all published works in the country. |
| `GET /api/v1/countries/<slug>/download/gpkg/` | GeoPackage download. |
| `GET /api/v1/countries/<slug>/download/csv/` | CSV download (WKT geometry column). |

All five endpoints accept a 2-letter ISO 3166-1 alpha-2 code (e.g. `DE`) and **301-redirect** to the canonical slug URL (e.g. `germany`). Use `?now` to bypass the cache (same as collection downloads). Continent and ocean pages do not have country-style feeds or downloads — they use the existing `/regions/continent/<slug>/` feed pages.

**Index / overview pages** (in the burger menu, `/pages/`, and the XML sitemap):

- `/countries/` — all countries grouped by continent; each continent header links to its continent landing page. Cross-linked with `/regions/` via buttons. (Distinct from `/in/`, which is the **sources** index.)
- `/at/` — umbrella places index: lists continents and oceans, and points to `/countries/` for the full country list (it does not duplicate it).
- `/in/` — all journals/sources with their published-work counts.
- `/at/<ISO>` (e.g. `/at/DE`) **301-redirects** to the canonical name slug (`/at/germany`) for every loaded country code.

`Source.slug` is auto-generated from the name on save and editable in the Source admin (each row links to its `/in/<slug>/` page). All four facet families plus source feeds appear in the sitemap. Coverage statistics are computed weekly into `SourceCoverageSnapshot` (no per-request computation); the `/statistics/` page links each source row, each **by-country** row (by ISO code → `/at/<country>/`), and each **by-journal** row (→ `/in/<source>/`) to its landing page. The `/works/` page carries a row of facet-exploration buttons.

### Sync external metadata

- `python manage.py sync_source_metadata` — syncs metadata from configured OAI-PMH endpoints back into the `Source` rows.
- `python manage.py update_openalex_sources` — enriches `Source` records from the OpenAlex API.
- When to re-run each (e.g. after adding a new source, on a quarterly cadence).

### EO4GEO BoK snapshot

OPTIMAP caches the public [EO4GEO Body of Knowledge](https://geospacebok.eu)
(also known as **GeoSpaceBoK**) so the autosuggest combobox on the work
landing page (issue #245) and the contribution endpoint can validate concept
codes without hitting upstream on every request. The cache is **lazy on miss**
— first request after deploy fetches and writes; the management command below
makes that explicit.

The BoK is served from a Firebase Realtime DB (`eo4geo-bok.firebaseio.com`)
and versioned. OPTIMAP is pinned to **`v9`** (the live version as of 2026-06,
1,212 concepts, verified 2026-06-11). Firebase also exposes a `current` alias,
but we use an explicit version to avoid silent drift when the alias moves.
`v9` reflects the live portal at [geospacebok.eu](https://geospacebok.eu) and
contains:
- An expanded GC3 AI/ML subtree (6 codes in v3 → 52 in v9, e.g.
  `GC3-11-2` Space-time dynamic reasoning, `GC3-14` Intelligent Software
  Agent).
- A new top-level `GN` category (GNSS – Global Navigation Satellite Systems)
  with ~277 sub-concepts.

**Settings (env vars, see `optimap/.env.example`):**

- `OPTIMAP_BOK_VERSION` — which BoK version to use. Default `v9`. Set to
  `v3`, `v8`, etc. to roll back to an older snapshot, or `current` to
  always track the latest live version.
- `OPTIMAP_BOK_API_BASE` — root of the Firebase API. Default
  `https://eo4geo-bok.firebaseio.com`.
- `OPTIMAP_BOK_CONCEPT_BASE_URL` — base URL for concept page links, e.g.
  `https://geospacebok.eu` (default) renders chips that link to
  `https://geospacebok.eu/<CODE>`. Change to `http://bok.eo4geo.eu` to
  use the legacy portal.
- `OPTIMAP_BOK_ENABLED_COLLECTIONS` — **opt-in allow-list** of
  `Collection.identifier` slugs (comma-separated, no spaces; e.g.
  `mountain-wetlands,essd`). The editor is shown only on works that
  belong to at least one of the listed collections; the
  `/contribute-bok/` endpoint enforces the same rule with 403.
  **Empty (default) = editor disabled site-wide** — list the
  collections you want to enable. Read-only chips on already-tagged
  works remain visible regardless. Update the env var and restart to
  apply; no migration needed.

**Refresh:**

```bash
python manage.py refresh_bok_snapshot                        # use settings.BOK_VERSION
python manage.py refresh_bok_snapshot --bok-version v9       # explicit version override
python manage.py refresh_bok_snapshot --dry-run              # fetch + report without writing
```

The snapshot lives in the `default` (DB) cache under
`bok:concepts:<version>:v1`. Clearing the cache forces a refetch on the
next request:

```bash
python manage.py clear_caches --cache default      # also drops other DB-cache rows
```

**When to refresh:**

- After a known upstream change (new concepts, renames).
- If the autosuggest input returns "No matches" for terms you expect.
- When upgrading `OPTIMAP_BOK_VERSION` (the old cached key stays until
  the cache is cleared; changing the version setting alone is not enough).

**Orphan codes.** If upstream removes a concept that's already stored on
a work, the chip on the landing page renders as a greyed plain-text chip
with a *"No longer in current GeoSpaceBoK"* tooltip. The code stays in
the DB so admins can decide whether to remove it, swap to a successor,
or wait for upstream to restore it.

### Operate the geoextent service

- Configuration knobs from CLAUDE.md §[Geoextent API Endpoints](../CLAUDE.md): `GEOEXTENT_MAX_FILE_SIZE_MB`, `GEOEXTENT_MAX_BATCH_SIZE_MB`, `GEOEXTENT_MAX_DOWNLOAD_SIZE_MB`, `GEOEXTENT_DOWNLOAD_WORKERS`.
- Known upstream bug (coordinate-order in `geoextent.from_remote()`); how to detect it in the wild.
- Where logs surface for failed remote extractions.

### Operate the OGC API - Features endpoint (`/ogcapi/`)

OPTIMAP exposes published works via [pygeoapi](https://pygeoapi.io/) at `/ogcapi/`, conforming to the [OGC API - Features Core](https://ogcapi.ogc.org/features/) standard. GIS clients (QGIS, R `sf`, Python `geopandas`) can connect directly — see [docs/ogcapi-clients.md](ogcapi-clients.md) for examples.

**How it works.** pygeoapi is mounted inside Django's URL routing (not a separate service). It connects directly to the same PostGIS database Django uses, via SQLAlchemy, reading from the `works_published` view (a `CREATE OR REPLACE VIEW` that filters `works_work` to `status = 'p'`). The endpoint is only active when `etc/pygeoapi-openapi.yml` exists.

**First-time setup / after config changes:**

```bash
python manage.py generate_pygeoapi_openapi
# Reads etc/pygeoapi-config.yml → writes etc/pygeoapi-openapi.yml.
# Use --force to overwrite an existing file.
```

This is run automatically with `--force` by `etc/manage-and-run.sh` on every Docker startup.

**Database credentials.** pygeoapi connects to the same database as Django: the connection is derived from `DATABASE_URL` and injected into the pygeoapi config at load time (`optimap/pygeoapi_db.apply_db_connection`). There is no separate database configuration for the OGC API — set `DATABASE_URL` correctly and a reachable database is all that is required (the works collection is introspected at generation time).

**Verify the endpoint is active:**

```bash
# Should print PYGEOAPI_ENABLED: True
python manage.py shell -c "from django.conf import settings; print('PYGEOAPI_ENABLED:', settings.PYGEOAPI_ENABLED)"

# Smoke test (follow the redirect on conformance/items)
curl -s http://localhost:8000/ogcapi/ | python -m json.tool
curl -sL http://localhost:8000/ogcapi/conformance | python -m json.tool
curl -sL "http://localhost:8000/ogcapi/collections/works/items?limit=2" | python -m json.tool
```

**Temporarily disable the endpoint** (e.g. to diagnose a startup problem) — rename or delete `etc/pygeoapi-openapi.yml`. Django will skip the `/ogcapi/` routes on the next restart and the rest of the app is unaffected.

**Regenerate after DB changes.** The OpenAPI document is generated from the `works_published` view's schema. If the view is dropped and recreated (e.g. after a migration that alters `works_work`), or if `etc/pygeoapi-config.yml` changes, regenerate with `--force`:

```bash
python manage.py generate_pygeoapi_openapi --force
# Then restart the server.
```

**Supported query parameters** on `/ogcapi/collections/works/items`:

| Parameter | Effect |
|-----------|--------|
| `bbox=minLon,minLat,maxLon,maxLat` | Spatial filter (WGS84) |
| `datetime=2023-01-01/2024-01-01` | Temporal filter on `publicationDate`; also accepts single date |
| `limit=N` | Page size (default 10) |
| `offset=N` | Pagination offset |

### Backup and restore

- `pg_dump` / `pg_restore` for the PostGIS database (geometry-aware).
- Fixtures in `fixtures/` for test data; not a substitute for backups.
- Static / media files (`OPTIMAP_DATA_DUMP_RETENTION`-rotated dumps in `/tmp/optimap_cache/` are regenerable, not backups).

### Upgrade and migration runbook

- Where the version is bumped ([optimap/\_\_init\_\_.py](../optimap/__init__.py)) and how it surfaces in the UI / API.
- Running migrations (`migrate` is auto-applied via `etc/manage-and-run.sh` in Docker).
- Reviewing [CHANGELOG.md](../CHANGELOG.md) before each upgrade — especially "Changed" / "Removed" entries that may require admin action (e.g. v0.12.0 bumped the harvest task's dotted path).
