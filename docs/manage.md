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
| | `issn_l`, `abbreviated_title` | Optional | Display only. |
| **Display metadata** | `publisher_name`, `homepage_url`, `is_oa`, `is_preprint`, `tags` | Optional | Display only — none of these affect harvesting. |
| **Statistics (auto-populated)** | `works_count`, `cited_by_count`, `last_harvest` | Read-only | Auto-populated. |

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

- **`url_field`** — currently **display only**. The harvester ignores it.
- ⚠️ **The DOI prefix is hard-coded to `10.5194` (Copernicus).** [`works/harvesting/crossref.py:335-339`](../works/harvesting/crossref.py) falls back to `"10.5194"` because `Source` has no `crossref_prefix` field today. **Adding a non-Copernicus crossref-prefix source through the admin alone will not work** — open an issue / extend the model if you need this.
- For the Copernicus path, leave `url_field` as something representative (e.g. `https://api.crossref.org/works?filter=prefix:10.5194`); harvest with `python manage.py harvest_journals --journal copernicus [--journal-title "<journal name>"]` to filter to a specific Copernicus journal.

##### Mountain Wetlands Repository (`mountain-wetlands`)

- **`url_field`** — MaRESS API endpoint. Example: `https://andes.mountain-wetlands-repository.info/api/v1/items/`.
- Bespoke harvester (`works.tasks.harvest_mountain_wetlands`); see [works/harvesting/mountain_wetlands.py](../works/harvesting/mountain_wetlands.py).

##### OpenAlex source (`openalex`)

The harvester (`works.tasks.harvest_openalex_source`) needs the OpenAlex Source identifier `S<digits>`. It looks for an `S<digits>` substring in two fields, in this order — **first match wins**:

1. `openalex_id` — recommended. Set to the bare ID, e.g. `S4210203054`, or the full URL `https://openalex.org/S4210203054`.
2. `url_field` — fallback. Any URL containing the ID works (e.g. `https://api.openalex.org/sources/S4210203054`).

The public Source API exposes a derived `openalex_url` (`https://openalex.org/<S-id>`) computed from `openalex_id`; it is no longer a stored field, so there is no second OpenAlex field to keep in sync.

Minimum-viable example for **AGILE GIScience Series**:

| Field | Value |
|-------|-------|
| `name` | `AGILE GIScience Series (OpenAlex)` |
| `source_type` | `openalex` |
| `url_field` | `https://api.openalex.org/sources/S4210203054` *(any placeholder works as long as `openalex_id` is set)* |
| `openalex_id` | `S4210203054` |
| `default_work_type` | `proceedings-article` |
| `is_oa` | ✓ *(display flag)* |
| `harvest_interval_minutes` | `0` *(start manual, raise once a smoke run succeeds)* |
| `collection` | optional — pick or create `agile-giss` |
| `publisher_name`, `homepage_url` | optional display fields |

> **Common error:** if you create the source with `source_type=oai-pmh` and the AGILE-GISS OAI URL (`https://oai-pmh.copernicus.org/oai.php?…&set=agile-giss`), the harvester will fail with HTTP 404 — Copernicus's OAI-PMH endpoint has been dark since 2025-12. Switch `source_type` to `openalex` and fill the OpenAlex fields above.
> **Faster than typing it in:** `python manage.py harvest_journals --insert-sources` creates this exact AGILE-GISS-OpenAlex row (and every other built-in entry from `SOURCE_CONFIG`) idempotently — see "Bootstrap the admin from the journal config" below. Only do the manual admin route when you need a source that's not in `SOURCE_CONFIG`.

> **Source.collection is optional at create time, but always populated by the time the first harvest finishes.** Leaving the collection field blank when you create a Source is **not** an error:
> - **OAI-PMH / OJS / Janeway sources** auto-create a Collection on first harvest, slugged from the source name (e.g. `"Earth System Science Data"` → identifier `earth-system-science-data`). The new Collection starts **unpublished** so it does not show up on `/collections/` until you review the auto-derived name and description and flip `is_published`. Review auto-created collections at `/admin/works/collection/?is_published__exact=0` before publishing.
> - **RSS / Crossref / MaRESS / OpenAlex sources** don't auto-create — they get their Collection from `harvest_journals --insert-sources` instead. If you leave `Source.collection` blank for one of these and run a harvest, the works simply aren't added to any collection, and curators can add them by hand from each work landing page later.
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

CLI alternatives still work and are documented in README §[Harvest Publications from real journals](../README.md#harvest-publications-from-real-journals): `python manage.py harvest_journals --journal <slug>` (with `--list`, `--all`, `--create-sources`, `--user-email`, `--max-records`).

**Recover from a thundering-herd schedule state:** `python manage.py reset_harvest_schedules` rebuilds every `Harvest Source <id>` recurring schedule with a properly deferred `next_run` and (by default) staggers them across the smallest harvest interval so the cluster doesn't get hit with every source at once. Use this after a bulk `--insert-sources` run on a deployment that pre-dated the `Source.save()` fix, or any time you find every source firing simultaneously. Flags: `--dry-run` (preview), `--no-stagger` (set every `next_run` to `now + its own interval`), `--clear-manual` (also delete leftover `Manual Harvest Source <id>` one-off rows from the admin "Schedule harvesting" action).

**Bootstrap the admin from the journal config:** `python manage.py harvest_journals --insert-sources` creates one `Source` row per (enabled) entry in `harvest_journals`'s `SOURCE_CONFIG` without harvesting. Each insert also gets-or-creates a `Collection` from the entry's `collection_name`. After running it, every configured journal appears at `/admin/works/source/` (linked to its collection) and can be triggered with the actions above. Re-running is idempotent (existing rows by name or URL are left alone). Add `--include-disabled` to insert journals whose upstream is currently broken (e.g. the Copernicus OAI-PMH 404). All inserts default to `harvest_interval_minutes = 0` (manual-only) so the cluster is not flooded after a bulk insert; `Source.save()` dispatches to the correct task per source type when you later raise the interval.

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
| Different `Source` | n/a | Skipped with an info log message. **Cross-source merging is not handled.** |

> **OPTIMAP does not currently merge metadata across sources.** When the same DOI/URL is exposed by two different sources (e.g. a journal article also surfaced via a preprint server), the second source's harvest is logged and skipped — the first source "owns" the Work. There is no automatic union of fields, no cross-source provenance trail, and no way to switch ownership without manual admin intervention. If this becomes a frequent need, open a follow-up issue.

#### Careful update (`--update` flag)

`python manage.py harvest_journals --update` (or `update_existing=True` on the task functions) refreshes existing same-source works in place. The update is deliberately conservative:

- **Preserved when the new harvest brings nothing for them:** `geometry`, `timeperiod_startdate`, `timeperiod_enddate`. These often come from a user contribution through OPTIMAP that the source still does not provide; we don't want to wipe a curator's work because the upstream record is silent on coordinates.
- **Never overwritten:** `status` (a Published Work stays Published, never flips back to Harvested) and `created_by` (audit trail).
- **Refreshed from the new harvest:** title, abstract, authors, keywords, topics, OpenAlex enrichment fields, the `provenance.harvest` and `provenance.metadata_sources` and `provenance.openalex_match` sections, and the `Source` FK.
- **Audit trail:** a `harvest_update` event is appended to `Work.provenance.events` (existing events including user contributions are preserved).

Use `--update` when you want OpenAlex enrichment to re-run on previously-harvested works (e.g. after a matcher change), or when an upstream metadata change should propagate without losing curator additions. Without it, re-running the harvester is a no-op for already-known records.

### Email notifications on completion / failure

`harvest_oai_endpoint` sends a result email to the user that triggered the run (the user who clicked the action; falls back to silently skipping if there is no user). Subject lines are `✅ Harvesting Completed for <collection>` or `❌ Harvesting Failed for <collection>`. To debug locally, point Django at the console backend in `.env`:

```env
EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
```

### Work-state-change notifications

Separate from harvest emails, OPTIMAP sends notification emails when a user-visible *Work* state change happens. The dispatcher lives in [`works/notifications.py`](../works/notifications.py); the event registry is `WORK_EVENT_HANDLERS`.

| Event | Recipients | Body highlights |
| --- | --- | --- |
| `contribution` | all `is_staff` users + every curator of every `Collection` the work is in (minus the contributor) | work title + DOI + link to `/work/<identifier>/`; *transparency block* with **roles + counts** of the other notified parties (`Notified: 1 admin and 2 curators of 'Mountain Wetlands', 'AGILE-GISS'`); heads-up that any of them can publish the work concurrently. |
| `publish` | every distinct `Contribution.user` for the work (minus the actor doing the publish) | "thank you, your work is now public" + title + DOI + their contribution kinds + the public landing-page URL. |

Both events route through Django-Q (`async_task`) so the request that triggered the state change stays fast. Recipient resolution happens synchronously in the caller's transaction — the queue payload is just a list of user IDs.

Republish suppression: `notify_work_event(work, "publish", …)` stamps `provenance.publication_notified_at` after the first fan-out and returns early on subsequent calls, so a publish→unpublish→republish cycle does not re-notify contributors.

**To add a new state-change notification** — e.g. notify the original contributors when an admin *unpublishes* their work — write a private `_enqueue_<event>(work, actor)` function (resolves recipients + calls `async_task` on a sibling `send_*` task), add it to `WORK_EVENT_HANDLERS`, and call `notify_work_event(work, "<event>", actor=request.user)` after the relevant `work.save()`. The dispatcher is best-effort: any handler exception is logged but never crashes the state change.

### Where things live in code

For maintainers cross-referencing the admin features above:

- Admin classes & actions: [works/admin.py](../works/admin.py) — `SourceAdmin`, `HarvestingEventAdmin`, `RecentHarvestingEventInline`, `_enqueue_harvest`, `trigger_harvesting_for_specific`, `trigger_harvesting_for_all`, `schedule_harvesting`, `retry_event`.
- Harvesters: [works/harvesting/](../works/harvesting/) — one module per source type (`oai.py`, `rss.py`, `crossref.py`, `mountain_wetlands.py`), with shared helpers in `common.py` (HarvestStats, dedup helpers, `complete_harvest` / `fail_harvest` / `send_harvest_email`), `sessions.py` (HTTP session factories), `metadata_html.py` (geometry + temporal extraction), and `openalex.py`. Persists `records_added`, `records_updated`, `records_with_spatial`, `records_with_temporal`, `log_text`, and `error_message` (truncated to 1000 chars) on the event. The public entry points are re-exported from [works/tasks.py](../works/tasks.py) so Django-Q dotted-path schedules (e.g. `works.tasks.harvest_oai_endpoint`) keep working.
- Models: [works/models.py](../works/models.py) — `Source`, `HarvestingEvent` (`error_message`, `log_text`, `records_added`, `records_with_spatial`, `records_with_temporal`; index on `(source, -started_at)`).
- Migration: [works/migrations/0003_harvestingevent_error_message_and_more.py](../works/migrations/0003_harvestingevent_error_message_and_more.py).
- Tests: [tests/test_admin_harvesting.py](../tests/test_admin_harvesting.py), [tests/test_regular_harvesting.py](../tests/test_regular_harvesting.py).

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
- on `/collections/<identifier>/` — a top-of-page banner with the same controls;
- on every work landing page — for users who curate at least one collection, an "Add to / Remove from" button per applicable collection.

These mirror the per-work admin controls on the work landing page (Publish / Unpublish / Edit in Admin), keeping the workflow consistent for both admins and curators.

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
| `crossref-prefix` | `harvest_crossref_prefix` | Crossref `works` API filtered by DOI prefix |
| `mountain-wetlands` | `harvest_mountain_wetlands` | Bespoke harvester for the Mountain Wetlands Repository (MaRESS) |
| `openalex` | `harvest_openalex_source` | OpenAlex `works` API filtered by `primary_location.source.id` |

`oai-pmh`, `ojs`, and `janeway` share the same harvester today; the distinction captures the platform so the metadata extractor's priority order (schema.org JSON-LD → `geo+json` link → `DC.SpatialCoverage` → `DC.box`) and admin UI can branch in future without another migration.

#### OpenAlex-as-source — `openalex` source type

OpenAlex is most useful in OPTIMAP as an *enrichment* layer (DOI-based matching during harvest), but for journals where the upstream OAI-PMH endpoint is unreliable and the Crossref payload is bibliographic-only (e.g. Copernicus journals, where the OAI-PMH endpoint at `oai-pmh.copernicus.org/oai.php` has been HTTP 404 since 2025-12), OpenAlex is also the most complete data source available. The `openalex` source type makes that an explicit, schedulable harvest path.

- **Identifier:** the harvester pulls `https://api.openalex.org/works?filter=primary_location.source.id:<S-id>` where `<S-id>` is taken (in order) from `Source.openalex_id` or `Source.url_field` — anything containing the `S<digits>` token works. Set the bare ID (e.g. `S4210203054` for AGILE GIScience Series) on the Source change page; the public Source API derives the `openalex_url` (`https://openalex.org/<S-id>`) from it on the fly.
- **Pagination:** cursor-based (`cursor=*`), 200 records per page, polite-pool User-Agent. Honors `--max-records` and accepts a `sort` kwarg (`publication_date:desc` is the default for the comparison command, unset for production runs).
- **What you get from OpenAlex:** title, abstract (reconstructed from `abstract_inverted_index`), publication date, authors, keywords, AI-derived topics, biblio (volume / issue / pages), `openalex_id`, `openalex_ids` (DOI / PMID / etc.), `openalex_open_access_status`, `openalex_fulltext_origin`, `openalex_is_retracted`, work type.
- **What you don't get:** OpenAlex carries no spatial or temporal coverage. The harvester deliberately does **not** fetch publisher landing pages — for AGILE-GISS we verified that the Copernicus landing pages also carry no `DC.SpatialCoverage` / `DC.box` / schema.org `spatialCoverage` / `geo+json` link, so the round-trip would be wasted work. If you point the harvester at a journal whose landing pages *do* carry spatial metadata, leave a follow-up issue: a per-source toggle for landing-page extraction is the obvious extension.
- **Run it manually:** `python manage.py harvest_journals --journal agile-giss-openalex --create-sources` (or trigger from the admin once the Source row exists).
- **Compare against the Crossref route:** `python manage.py compare_agile_giss_routes` runs both harvesters end-to-end inside a transaction that is rolled back at the end (savepoint-isolated so the second route doesn't see the first route's writes). Useful any time you change the harvester or want to A/B-test field coverage on a new journal.

#### Mountain Wetlands Repository (MaRESS) — `mountain-wetlands` source type

The MaRESS harvester is bespoke because the API is Zotero-shaped, not OAI-PMH/RSS/Crossref:

- **Run it manually:** `python manage.py harvest_journals --journal mountain-wetlands` (also available as a one-click admin action on the Source). Auto-scheduling is intentionally off — `harvest_interval_minutes` defaults to 0 for this source type and the issue (#192) requires the harvest to be manual.
- **Geometry:** built from each item's `study_sites[].location.{latitude, longitude}`. One Point per site, wrapped in a `GeometryCollection`. Records without sites get an empty geometry.
- **Dates:** the API's `date` field is free-text and often year-only (e.g. `"1993"`). The harvester parses the four-digit prefix and stores Jan 1 of that year; both `timeperiod_startdate` and `_enddate` are set to the year string.
- **DOI / OpenAlex enrichment:** every MaRESS record currently has `DOI=null` and an empty `url`, so OpenAlex is the *only* path to a DOI. The harvester calls `build_openalex_fields(title, doi=None, author=<first author surname>)`. Results land in `Work.provenance.openalex_match.status`:
  - `verified` — strong title+author match; DOI extracted from `openalex_ids` and saved on the Work,
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

## Reference-manager / Zotero compatibility

Work landing pages (`/work/<id>/` and `/work/<doi>/`) and collection detail pages (`/collections/<id>/`) emit the metadata that the [Zotero browser connector](https://www.zotero.org/download/connectors) and other reference managers (Mendeley, ReadCube, Citation Web Linker, etc.) read. No setup required — when a reader visits a work landing page with the connector installed, the connector recognises it as a journal article and offers "Save to Zotero". On a published collection page it offers "Save to Zotero (multiple items)" so a curator's curated set can be imported in one click.

What populates in the reader's reference manager (when the OPTIMAP record has the data): title, authors, publication date, DOI, journal title, ISSN, abstract, keywords, language, publisher, volume, issue, page range, and a PDF URL when the harvested URL ends in `.pdf`. Volume / issue / page range are populated by the OpenAlex matcher only — the OAI-PMH, RSS, Crossref, and MaRESS harvesters do not currently capture them, so works that never matched against OpenAlex will be missing those four fields. The mechanics are Highwire Press `citation_*` meta tags + `ScholarlyArticle` JSON-LD + a COinS span fallback, all built in [works/seo.py](../works/seo.py) and rendered from [works/templates/work_landing_page.html](../works/templates/work_landing_page.html) and [works/templates/collection_page.html](../works/templates/collection_page.html).

### Geotagging meta tags

Work landing pages also emit the conventional [HTML *Geotagging* meta tags](https://en.wikipedia.org/wiki/Geotagging#HTML_pages) so map-aware crawlers and indexers can discover the work's geographic coverage without parsing the JSON-LD payload:

- `geo.position` — `"lat;lon"` of the geometry's bounding-box centroid.
- `ICBM` — `"lat, lon"`, the [Yahoo variant](https://en.wikipedia.org/wiki/ICBM_address#Modern_use). Both tags are emitted when geometry is present.
- `geo.placename` — the human-readable Nominatim hierarchy (e.g. *"Sulawesi, Indonesia"*), only when `Work.placename` is set.
- `geo.region` — ISO 3166-1 alpha-2 country code (e.g. `ID`), only when `Work.country_code` is set.

The corresponding [schema.org `Place.geo`](https://schema.org/geo) payload follows the spec: single-point geometries are emitted as `GeoCoordinates`, anything else as `GeoShape` with `box="south west north east"` (matching the format already used for region feed pages).

## Reverse geocoding (placename / country backfill)

`Work.placename` and `Work.country_code` are populated by reverse-geocoding the work's geometry via [Nominatim](https://nominatim.openstreetmap.org/). For multi-point geometries — e.g. the Mountain Wetlands harvester emits one `Point` per study site — every representative point is geocoded separately and the result is reduced to the **lowest common ancestor** in the Nominatim address hierarchy. So a work with sites in Berlin and Munich resolves to *"Germany"* / `DE` (state diverges, country shared); a work spanning Germany and France resolves to `(None, None)` rather than the misleading geometric centroid in northern France. Single-Polygon works contribute one interior representative point. The walk is capped at 20 points so a 500-vertex polygon doesn't trigger 500 Nominatim requests.

Reverse geocoding is **on by default in production** so the `geo.placename` / `geo.region` HTML meta tags are emitted and `Work.provenance.geocoding` is populated by every harvester — set `OPTIMAP_GEOCODE_WORKS_ON_SAVE=False` in the deployment environment to opt out (e.g. for a bulk import where you would rather backfill placenames separately afterwards). The setting is forced off under the test runner regardless, so the suite stays offline. With the flag on, every `Work.save()` (creation or geometry edit) calls `works.services.geocoding.geocode_geometry(geom)` from a `pre_save` signal — each per-point lookup is cached in the per-process `LocMemCache` (key `reverse_geocode:<lat>:<lon>` with 3-decimal-place quantisation, ~100 m, 30-day TTL) so popular regions hit memory after the first lookup, keeping sustained Nominatim traffic well below the 1 req/s courtesy limit. On a complete geocoding outage (no point returned an address) the existing fields are preserved; on a real "geometry spans incompatible regions" outcome the fields are honestly cleared.

For an existing deployment that wants to populate the new fields retroactively, run the backfill:

```bash
python manage.py backfill_placenames                  # all works missing placename
python manage.py backfill_placenames --limit 200      # batch the first 200
python manage.py backfill_placenames --dry-run        # preview only, no DB writes
python manage.py backfill_placenames --force          # re-fetch existing entries too
python manage.py backfill_placenames --sleep 1.5      # increase courtesy delay
```

The command sleeps `--sleep` seconds (default 1.1) between cache *misses* to honour Nominatim's [1 req/s usage policy](https://operations.osmfoundation.org/policies/nominatim/). Cache hits are free. Failures (network errors, no result) are logged at WARNING level and leave the existing fields untouched — the meta tags simply omit `geo.placename` / `geo.region` until a successful lookup persists. Customise the User-Agent string via `OPTIMAP_GEOCODER_USER_AGENT`.

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

**Common failure modes:**

- **Stale dotted paths.** Pre-v0.12.0 schedules referenced `publications.tasks.*` instead of `works.tasks.*`. Long-lived deployments may still have these — the cluster fails them with `ImportError`. Delete them from `/admin/django_q/schedule/` and re-create them by saving the corresponding `Source` (or run `python manage.py reset_harvest_schedules`).
- **Thundering herd after `harvest_journals --insert-sources`.** Pre-fix `Source.save()` created Schedule rows with `next_run = now`. Recover with `python manage.py reset_harvest_schedules` (see "Manage harvesting" → "Recover from a thundering-herd schedule state").
- **Cluster down, queue grows.** Restart `qcluster` and watch `qinfo` — the queue drains in roughly the order tasks were enqueued. To skip the backlog, truncate `django_q_ormq` from the dbshell or via the `/admin/django_q/` views.

---

## Suggested further sections

The following sections are **suggested, not yet written**. They cover the rest of the admin surface and are worth filling in as the corresponding features stabilise. Each entry lists what the section should cover and the relevant code/admin URLs so an author can pick one up without further investigation.

### Manage works (publications)

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

### Manage Zenodo data deposition

Issue #63 — the full cycle is: regenerate the public data dumps, render `data/README.md` + a versioned `data/optimap-main.zip` + `data/zenodo_dynamic.json`, then upload/update a Zenodo draft. **Publishing the draft remains a manual step** (admins receive an email with the draft URL).

**Settings (env or `.env`):**

- `ZENODO_API_TOKEN` — Zenodo (or sandbox) personal access token. Required.
- `ZENODO_API_BASE` — defaults to `https://sandbox.zenodo.org/api`. Set to `https://zenodo.org/api` for production.
- `ZENODO_SANDBOX_DEPOSITION_ID` — optional. If unset, the deposit step **reuses the latest successful `ZenodoDepositionLog.deposition_id`** for the target `api_base`; if there is no prior log either, it **bootstraps a fresh draft** via `POST /deposit/depositions`. Sandbox and production each track their own counter.

**Trigger a deposition manually:**

```bash
# Combined render + deposit (no flags = use settings/env)
python manage.py zenodo_deposit
# Just render the files into data/ (no API call)
python manage.py render_zenodo
# Just upload to (or bootstrap) the draft
python manage.py deposit_zenodo
# Pin a specific draft (skips the resolver/bootstrap)
python manage.py deposit_zenodo --deposition-id 123456
```

The combined command is also available as an **admin action** on any of the work-list admin pages: *Trigger Zenodo Deposition*.

**Schedule annual auto-deposit:**

```bash
python manage.py schedule_zenodo_deposit
# Idempotent — registers works.tasks.run_zenodo_deposition for Dec 31 23:59 yearly
```

The scheduled task chains `regenerate_all_data_dumps → render_zenodo_package → deposit_to_zenodo`, so the deposit always reflects the latest data dumps. Re-running the command is a no-op when the schedule already exists.

**Lifecycle the deposit code handles automatically:**

1. **First run, nothing configured** → POSTs to `/deposit/depositions`, gets a fresh draft ID, logs it, uploads files.
2. **Subsequent runs against the same draft** → reuses the latest log row's ID, deletes inherited files, re-uploads, re-PUTs metadata.
3. **Run after admin has manually published the draft** → detects `submitted=true` + `state="done"`, calls `POST .../actions/newversion`, switches to the new draft from `links.latest_draft`, then proceeds as in (2).
4. **Grants metadata rejected** (Zenodo's curated vocabulary doesn't list OPTIMETA/KOMET yet) → retries once without `grants` and appends a free-text funding statement to `metadata.notes`. Logged in the `notes` field of the log row.

**Inspect a deposition:** `/admin/works/zenododepositionlog/` — status, file list with sizes, merged metadata diff, upload duration, error traceback when it failed, and a direct link to the Zenodo draft. The `/data/` page on the public site shows the latest *successful* deposition (sandbox-only when `DEBUG=True`, production-only otherwise).

### Manage data dumps and caches

#### Data dump cache

Cached files in `/tmp/optimap_cache/`; retention controlled by `OPTIMAP_DATA_DUMP_RETENTION` (default: 3 *cycles* — each cycle writes `.geojson`, `.geojson.gz`, `.gpkg`, and `.csv` for the same timestamp).

- The umbrella `regenerate_all_data_dumps` task runs every `DATA_DUMP_INTERVAL_HOURS` hours (default 6, see `optimap/settings.py`). It serialises published works to GeoJSON once and converts the same intermediate to GeoPackage and CSV via `ogr2ogr`. The schedule is created on `post_migrate` (`works.apps.schedule_data_dump`); legacy single-format schedules are removed automatically.
- Force a regenerate from a shell:
  ```bash
  python manage.py regenerate_data_dumps                  # all three formats (umbrella)
  python manage.py regenerate_data_dumps --format csv     # only CSV (also: geojson | gpkg)
  python manage.py regenerate_data_dumps --dry-run        # report without writing
  ```
  Runs synchronously in-process — does not need the Q cluster, useful in deploy scripts and for ad-hoc debugging. The same operation is also available via Django-Q (`async_task('works.tasks.regenerate_all_data_dumps')`) and via the admin **Works → action "Regenerate all data exports now"**.
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

### Sync external metadata

- `python manage.py sync_source_metadata` — syncs metadata from configured OAI-PMH endpoints back into the `Source` rows.
- `python manage.py update_openalex_journals` — enriches `Source` records from the OpenAlex API.
- When to re-run each (e.g. after adding a new source, on a quarterly cadence).

### EO4GEO BoK snapshot

OPTIMAP caches the public [EO4GEO Body of Knowledge](https://eo4geo.eu/bok/)
so the autosuggest combobox on the work landing page (issue #245) and the
contribution endpoint can validate concept codes without hitting upstream
on every request. The cache is **lazy on miss** — first request after
deploy fetches and writes; the management command below makes that
explicit.

**Settings (env vars, see `optimap/.env.example`):**

- `OPTIMAP_BOK_VERSION` — which BoK version to follow. Default `v3`
  (pinned for label stability). Set to `current` to auto-follow
  upstream renames/additions; pin to another `vN` if upstream churn
  breaks chips.
- `OPTIMAP_BOK_API_BASE` — root of the Firebase API. Default
  `https://eo4geo-bok.firebaseio.com`.
- `OPTIMAP_BOK_CONCEPT_BASE_URL` — base for the public concept pages.
  Default `http://bok.eo4geo.eu`. Used as a fallback when a concept's
  `uri` field is missing.
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
python manage.py refresh_bok_snapshot               # use settings.BOK_VERSION
python manage.py refresh_bok_snapshot --version v3  # pin a version explicitly
python manage.py refresh_bok_snapshot --dry-run     # fetch + report without writing
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
- On a quarterly cadence (mostly to keep names in sync with upstream).

**Orphan codes.** If upstream removes a concept that's already stored on
a work, the chip on the landing page renders as a greyed plain-text chip
with a *"No longer in current EO4GEO BoK"* tooltip. The code stays in
the DB so admins can decide whether to remove it, swap to a successor,
or wait for upstream to restore it.

### Operate the geoextent service

- Configuration knobs from CLAUDE.md §[Geoextent API Endpoints](../CLAUDE.md): `GEOEXTENT_MAX_FILE_SIZE_MB`, `GEOEXTENT_MAX_BATCH_SIZE_MB`, `GEOEXTENT_MAX_DOWNLOAD_SIZE_MB`, `GEOEXTENT_DOWNLOAD_WORKERS`.
- Known upstream bug (coordinate-order in `geoextent.from_remote()`); how to detect it in the wild.
- Where logs surface for failed remote extractions.

### Backup and restore

- `pg_dump` / `pg_restore` for the PostGIS database (geometry-aware).
- Fixtures in `fixtures/` for test data; not a substitute for backups.
- Static / media files (`OPTIMAP_DATA_DUMP_RETENTION`-rotated dumps in `/tmp/optimap_cache/` are regenerable, not backups).

### Upgrade and migration runbook

- Where the version is bumped ([optimap/\_\_init\_\_.py](../optimap/__init__.py)) and how it surfaces in the UI / API.
- Running migrations (`migrate` is auto-applied via `etc/manage-and-run.sh` in Docker).
- Reviewing [CHANGELOG.md](../CHANGELOG.md) before each upgrade — especially "Changed" / "Removed" entries that may require admin action (e.g. v0.12.0 bumped the harvest task's dotted path).
