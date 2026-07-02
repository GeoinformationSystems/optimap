# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import json

from django import template
from django.utils.html import escape, format_html, format_html_join
from django.utils.safestring import mark_safe

register = template.Library()


# https://stackoverflow.com/a/23783666/261210
@register.filter
def addstr(arg1, arg2):
    """concatenate arg1 & arg2"""
    return str(arg1) + str(arg2)


# Work type → Font Awesome icon (single source of truth, mirrored client-side in
# static/js/map-popup.js `publicationTypeIconHTML`). Physical samples (#187) get a
# vial, data a database, everything else a document. Keep the two in sync.
_WORK_TYPE_ICONS = {
    "physical-object": "fas fa-vial",
    "dataset": "fas fa-database",
    "database": "fas fa-database",
}
_WORK_TYPE_ICON_DEFAULT = "fas fa-file-lines"


@register.filter
def work_type_icon(work):
    """Return the Font Awesome class for a Work (or a bare type string)."""
    work_type = getattr(work, "type", work)
    return _WORK_TYPE_ICONS.get(work_type, _WORK_TYPE_ICON_DEFAULT)


# Human-readable tooltip for the placename's origin, keyed by the value recorded
# in Work.provenance.metadata_sources.placename. IGSN samples carry an authoritative
# place from DataCite / the GFZ landing page; everything else is reverse-geocoded.
_PLACENAME_SOURCE_TIPS = {
    "datacite.geoLocationPlace": (
        "This location name was supplied by the sample's registrant via DataCite (geoLocationPlace)."
    ),
    "gfz.landing_page": "This location name was read from the sample's GFZ IGSN landing page.",
}
_PLACENAME_NOMINATIM_TIP = (
    "This information was derived from the geometries of the work based on the Nominatim gazetteer."
)


@register.filter
def placename_source_tip(work):
    """Return the tooltip describing where a Work's placename came from (provenance-aware)."""
    provenance = getattr(work, "provenance", None)
    source = (provenance or {}).get("metadata_sources", {}).get("placename") if isinstance(provenance, dict) else None
    return _PLACENAME_SOURCE_TIPS.get(source, _PLACENAME_NOMINATIM_TIP)


@register.simple_tag
def render_provenance(provenance):
    """Render a Work.provenance JSON dict as readable HTML.

    Schema: see ``works/utils/provenance.py``.

    Anything we don't recognize is dumped as pretty-printed JSON at the
    bottom so curators can still see it.
    """
    if not provenance:
        return ""
    if not isinstance(provenance, dict):
        # Tolerate legacy text that escaped the migration.
        return format_html('<pre class="provenance-pre">{}</pre>', provenance)

    sections = []
    harvest = provenance.get("harvest")
    if isinstance(harvest, dict):
        rows = []
        for label, key in (
            ("Harvester", "harvester"),
            ("Source name", "source_name"),
            ("Source type", "source_type"),
            ("Source URL", "source_url"),
            ("Harvested at", "harvested_at"),
            ("Event ID", "harvesting_event_id"),
            ("DOI", "doi"),
        ):
            v = harvest.get(key)
            if v is None or v == "":
                continue
            rows.append((label, str(v)))
        if rows:
            sections.append(
                format_html(
                    '<h6 class="mt-2 mb-1">Harvest</h6><dl class="row mb-2 small">{}</dl>',
                    format_html_join("", '<dt class="col-sm-3 text-muted">{}</dt><dd class="col-sm-9">{}</dd>', rows),
                )
            )
        if isinstance(harvest.get("original_record"), (dict, list)):
            sections.append(
                format_html(
                    '<details class="small mb-2"><summary>Original record</summary>'
                    '<pre class="provenance-pre">{}</pre></details>',
                    json.dumps(harvest["original_record"], indent=2, sort_keys=True),
                )
            )

    metadata_sources = provenance.get("metadata_sources")
    if isinstance(metadata_sources, dict) and metadata_sources:
        rows = [(k, v) for k, v in sorted(metadata_sources.items())]
        sections.append(
            format_html(
                '<h6 class="mt-2 mb-1">Metadata sources</h6><dl class="row mb-2 small">{}</dl>',
                format_html_join("", '<dt class="col-sm-3 text-muted">{}</dt><dd class="col-sm-9">{}</dd>', rows),
            )
        )

    openalex_match = provenance.get("openalex_match")
    if isinstance(openalex_match, dict) and openalex_match:
        rows = [
            (k, json.dumps(v) if not isinstance(v, str) else v)
            for k, v in openalex_match.items()
            if k != "top_candidate"
        ]
        block = format_html(
            '<h6 class="mt-2 mb-1">OpenAlex match</h6><dl class="row mb-2 small">{}</dl>',
            format_html_join("", '<dt class="col-sm-3 text-muted">{}</dt><dd class="col-sm-9">{}</dd>', rows),
        )
        if openalex_match.get("top_candidate"):
            block = format_html(
                '{}<details class="small mb-2"><summary>Top candidate</summary>'
                '<pre class="provenance-pre">{}</pre></details>',
                block,
                json.dumps(openalex_match["top_candidate"], indent=2, sort_keys=True),
            )
        sections.append(block)

    openaire_match = provenance.get("openaire_match")
    if isinstance(openaire_match, dict) and openaire_match:
        rows = [(k, json.dumps(v) if not isinstance(v, str) else v) for k, v in openaire_match.items()]
        sections.append(
            format_html(
                '<h6 class="mt-2 mb-1">OpenAIRE match</h6><dl class="row mb-2 small">{}</dl>',
                format_html_join("", '<dt class="col-sm-3 text-muted">{}</dt><dd class="col-sm-9">{}</dd>', rows),
            )
        )

    geocoding = provenance.get("geocoding")
    if isinstance(geocoding, dict) and geocoding:
        rows = []
        for label, key in (
            ("Gazetteer", "gazetteer"),
            ("Gazetteer URL", "gazetteer_url"),
            ("Placename", "placename"),
            ("Points geocoded", "n_geocoded"),
            ("Geocoded at", "geocoded_at"),
        ):
            v = geocoding.get(key)
            if v is None or v == "":
                continue
            rows.append((label, str(v)))
        block = (
            format_html(
                '<h6 class="mt-2 mb-1">Reverse geocoding</h6>'
                '<p class="small text-muted mb-1">Placename derived from the work\'s geometries via the Nominatim gazetteer. Country association is derived separately by a point-in-polygon join with Natural Earth data (see Work.countries).</p>'
                '<dl class="row mb-2 small">{}</dl>',
                format_html_join("", '<dt class="col-sm-3 text-muted">{}</dt><dd class="col-sm-9">{}</dd>', rows),
            )
            if rows
            else ""
        )

        matches = geocoding.get("matches")
        if isinstance(matches, list) and matches:
            match_items = []
            for m in matches:
                if not isinstance(m, dict):
                    continue
                display = m.get("display_name") or "(no display name)"
                osm_url = m.get("osm_url")
                osm_type = m.get("osm_type")
                osm_id = m.get("osm_id")
                lat = m.get("lat")
                lon = m.get("lon")
                # Format: "(lat, lon) → display name [OSM relation/51477]"
                if osm_url and osm_type and osm_id:
                    osm_link = format_html(
                        ' <a href="{}" target="_blank" rel="noopener">OSM {}/{}</a>',
                        osm_url,
                        osm_type,
                        osm_id,
                    )
                else:
                    osm_link = ""
                match_items.append(
                    format_html(
                        "<li><code>({}, {})</code> → {}{}</li>",
                        lat,
                        lon,
                        display,
                        osm_link,
                    )
                )
            if match_items:
                block = format_html(
                    '{}<details class="small mb-2"><summary>Per-point Nominatim matches ({})</summary>'
                    '<ul class="small">{}</ul></details>',
                    block,
                    len(match_items),
                    mark_safe("".join(match_items)),
                )

        if block:
            sections.append(block)

    events = provenance.get("events")
    if isinstance(events, list) and events:
        rows = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            kind = escape(ev.get("type", "?"))
            at = escape(ev.get("at", ""))
            details = {k: v for k, v in ev.items() if k not in ("type", "at")}
            details_html = format_html_join(
                " ",
                '<span class="text-muted">{}</span>=<code>{}</code>',
                ((k, json.dumps(v) if not isinstance(v, str) else v) for k, v in details.items()),
            )
            rows.append(
                format_html(
                    '<li class="mb-1"><strong>{}</strong> <small class="text-muted">{}</small> {}</li>',
                    kind,
                    at,
                    details_html,
                )
            )
        sections.append(
            format_html(
                '<h6 class="mt-2 mb-1">Events</h6><ul class="list-unstyled small">{}</ul>',
                mark_safe("".join(rows)),
            )
        )

    # Anything else — show raw JSON so nothing is hidden.
    known = {"harvest", "metadata_sources", "openalex_match", "openaire_match", "geocoding", "events"}
    leftover = {k: v for k, v in provenance.items() if k not in known}
    if leftover:
        sections.append(
            format_html(
                '<details class="small"><summary>Other</summary><pre class="provenance-pre">{}</pre></details>',
                json.dumps(leftover, indent=2, sort_keys=True),
            )
        )

    if not sections:
        return ""
    return mark_safe('<div class="provenance-rendered">' + "".join(sections) + "</div>")


@register.inclusion_tag("partials/author_list.html")
def work_author_list(work):
    """Render the author list with ORCID iD links for a Work object.

    Usage in templates: {% work_author_list work %}
    Calls authors_for_display() which aligns Work.authors (display names) with
    Author rows via WorkAuthor rank; falls back to an empty list on any error.
    """
    try:
        from works.services.authors import authors_for_display

        return {"authors_display": authors_for_display(work)}
    except Exception:
        return {"authors_display": []}
