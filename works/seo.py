# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""SEO helpers for OPTIMAP — issues #22, #226.

Builds ``django-meta`` ``Meta`` objects and Google Scholar ``citation_*``
lists for work landing pages, the homepage, and feed pages. Templates emit
Open Graph + Twitter Card + schema.org JSON-LD via ``django-meta``; the
Scholar tags are rendered inline because django-meta does not model them.
"""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import quote, urljoin

from django.conf import settings
from django.urls import reverse
from meta.views import Meta

from works.utils.geometry import COORDINATE_PRECISION

_WS = re.compile(r"\s+")

# Static fallback used as og:image / twitter:image when a work has no geometry
# (and on the homepage / feed pages which have no per-item preview image).
# The file lives at works/static/img/og-fallback.png; Django's staticfiles
# system serves it at /static/img/og-fallback.png.
_OG_FALLBACK_IMAGE = "/static/img/og-fallback.png"


def _abs(request, path: str) -> str:
    """Build an absolute URL — prefers the request host so previews and
    canonical URLs work in dev (``127.0.0.1:8002``) without overriding
    settings."""
    if request is not None:
        return request.build_absolute_uri(path)
    return urljoin(getattr(settings, "BASE_URL", "http://localhost:8000/"), path)


def _truncate_for_description(text: str | None, n: int = 200) -> str:
    if not text:
        return ""
    cleaned = _WS.sub(" ", text).strip()
    if len(cleaned) <= n:
        return cleaned
    # Cut on a word boundary.
    cut = cleaned[: n - 1].rsplit(" ", 1)[0]
    return cut + "…"


def _publisher_name(work) -> str | None:
    """Publisher display name: the work's own ``publisher`` (e.g. from OpenAIRE
    enrichment) if set, else the harvesting source name. Shared by the JSON-LD,
    Highwire ``citation_*``, and COinS builders so they stay in sync."""
    if work.publisher:
        return work.publisher
    if work.source and getattr(work.source, "name", None):
        return str(work.source.name)
    return None


def _normalize_author_list(work) -> list[str]:
    """Return ``[\"Given Family\", …]`` from a Work's ArrayField, mirroring
    the human-readable form used on the landing page."""
    raw = work.authors or []
    out: list[str] = []
    for a in raw:
        name = (a or "").strip()
        if "," in name and not name.endswith(","):
            family, given = (s.strip() for s in name.split(",", 1))
            name = f"{given} {family}".strip()
        if name:
            out.append(name)
    return out


def _author_records_for_schema(work) -> list[dict]:
    """Return ``[{"name": str, "orcid": str | None}, …]`` for schema.org output.

    Prefers ``WorkAuthor`` rows (names + ORCIDs from enrichment) when they
    cover at least as many authors as the display cache, matching the logic in
    ``authors_for_display``. Falls back to ``_normalize_author_list`` plain
    names when no identity rows exist.
    """
    try:
        wa_qs = work.work_authors.select_related("author").order_by("rank")
        ranked = [(wa.author.name, wa.author.orcid) for wa in wa_qs]
    except Exception:
        ranked = []

    plain_names = _normalize_author_list(work)

    if ranked and len(ranked) >= len(plain_names):
        return [{"name": name, "orcid": orcid} for name, orcid in ranked]

    if ranked:
        orcid_by_index = {i: orcid for i, (_, orcid) in enumerate(ranked) if orcid}
        name_map = {name.lower(): orcid for name, orcid in ranked if orcid}
        return [
            {"name": name, "orcid": orcid_by_index.get(i) or name_map.get(name.lower())}
            for i, name in enumerate(plain_names)
        ]

    return [{"name": name, "orcid": None} for name in plain_names]


def build_schema_org_for_work(work, request) -> dict:
    """Just the schema.org ``ScholarlyArticle`` JSON-LD dict — the heavy
    part of ``build_work_meta``. Pulled out so callers (e.g. the
    landing-page cache in ``works/views/work_views.py``) can cache the
    expensive output and pass it back into ``build_work_meta`` to skip
    the rebuild."""
    description = _truncate_for_description(work.abstract, n=200)
    canonical = _abs(request, reverse("optimap:work-landing", args=[work.get_identifier()]))
    has_geom = bool(work.geometry and not work.geometry.empty)
    image = None
    if has_geom:
        image = _abs(
            request,
            reverse("optimap:work-preview", args=[work.get_identifier()]),
        )
    keywords: list[str] = []
    if work.keywords:
        keywords.extend(k for k in work.keywords if k)
    if work.topics:
        keywords.extend(t for t in work.topics if t)
    author_records = _author_records_for_schema(work)
    return _build_schema_org(work, request, canonical, image, author_records, keywords, description)


def build_work_meta(request, work, *, kwargs_schema: dict | None = None) -> Meta:
    """``Meta`` for a work landing page. Includes Open Graph, Twitter Card,
    and schema.org ``ScholarlyArticle`` JSON-LD. When the work has geometry
    the map preview PNG (1200×630) is used as ``og:image``; otherwise the
    static OPTIMAP branded fallback (``og-fallback.png``) is used instead.

    ``kwargs_schema``: when set, used in place of recomputing the
    schema.org dict. Callers caching the heavy schema between requests
    pass it here to skip the PostGIS roundtrip."""
    title = work.title or "Untitled work"
    description = _truncate_for_description(work.abstract, n=200)
    canonical = _abs(request, reverse("optimap:work-landing", args=[work.get_identifier()]))

    has_geom = bool(work.geometry and not work.geometry.empty)
    if has_geom:
        image = _abs(
            request,
            reverse("optimap:work-preview", args=[work.get_identifier()]),
        )
    else:
        image = _abs(request, _OG_FALLBACK_IMAGE)

    keywords: list[str] = []
    if work.keywords:
        keywords.extend(k for k in work.keywords if k)
    if work.topics:
        keywords.extend(t for t in work.topics if t)

    author_records = _author_records_for_schema(work)

    # ``schema`` is the heavy bit (PostGIS roundtrip for the geojson dump
    # plus nested dict construction). Callers may pass in a precomputed
    # value to skip the rebuild — see issue #180 for the work_landing
    # context cache that uses this hook.
    schema = (
        kwargs_schema
        if kwargs_schema is not None
        else _build_schema_org(work, request, canonical, image, author_records, keywords, description)
    )

    meta = Meta(
        request=request,
        title=title,
        description=description,
        keywords=keywords or None,
        url=canonical,
        image=image,
        object_type="article",
        site_name="OPTIMAP",
        twitter_creator=None,
        twitter_site="@OPTIMAP",
        schemaorg_title=title,
        schemaorg_description=description,
        custom_namespace="article",
    )
    # Map preview is 1200x630 — set the OG hints so previews render at full size.
    if image:
        meta.image_width = 1200
        meta.image_height = 630
    # Article-typed Open Graph extras.
    extra: list[tuple[str, str, str]] = [
        ("property", "og:type", "article"),
    ]
    if work.publicationDate:
        extra.append(("property", "article:published_time", work.publicationDate.isoformat()))
    for rec in author_records:
        extra.append(("property", "article:author", rec["name"]))
    if work.source and getattr(work.source, "name", None):
        extra.append(("property", "article:section", str(work.source.name)))
    meta.extra_props = {tag: val for _, tag, val in extra}  # property=… name=… handled below
    meta.extra_custom_props = extra
    meta.schema = schema
    return meta


def external_identifier_links(work) -> list[dict]:
    """External canonical URLs that identify this work elsewhere, in priority
    order: DOI, OpenAlex, OpenAIRE, Wikidata.

    Each entry is ``{"href", "title"}``. Used both for the schema.org ``sameAs``
    relationships and for the HTML ``<link rel="alternate">`` tags, so the two
    stay in sync. Only identifiers that exist on the work are included.
    """
    links: list[dict] = []
    if work.doi:
        links.append({"href": f"https://doi.org/{work.doi}", "title": "DOI"})
    if work.openalex_id:
        oa = work.openalex_id
        links.append({"href": oa if oa.startswith("http") else f"https://openalex.org/{oa}", "title": "OpenAlex"})
    if work.openaire_url:
        links.append({"href": work.openaire_url, "title": "OpenAIRE"})
    wikidata_url = (
        work.wikidata_exports.filter(action__in=["created", "updated"], wikidata_url__isnull=False)
        .order_by("-export_date")
        .values_list("wikidata_url", flat=True)
        .first()
    )
    if wikidata_url:
        links.append({"href": wikidata_url, "title": "Wikidata"})
    return links


def _build_schema_org(work, request, canonical, image, author_records, keywords, description) -> dict:
    """Schema.org ``ScholarlyArticle`` JSON-LD. Mirrors what we *consume* from
    Janeway in ``works/tasks.py`` — closing the loop: we now publish the same
    metadata vocabulary that we read.

    ``author_records`` is a list of ``{"name": str, "orcid": str | None}``
    dicts from ``_author_records_for_schema``; ORCID-bearing authors get a
    ``sameAs`` pointing at ``https://orcid.org/<orcid>``."""
    payload: dict = {
        "@context": "https://schema.org",
        "@type": "ScholarlyArticle",
        "name": work.title,
        "headline": work.title,
        "url": canonical,
    }
    if description:
        payload["description"] = description
    if work.doi:
        payload["identifier"] = f"doi:{work.doi}"
    same_as = [link["href"] for link in external_identifier_links(work)]
    if same_as:
        payload["sameAs"] = same_as if len(same_as) > 1 else same_as[0]
    if work.publicationDate:
        payload["datePublished"] = work.publicationDate.isoformat()
    if author_records:

        def _person(rec):
            p = {"@type": "Person", "name": rec["name"]}
            if rec.get("orcid"):
                p["sameAs"] = f"https://orcid.org/{rec['orcid']}"
            return p

        payload["author"] = [_person(r) for r in author_records]
    if keywords:
        payload["keywords"] = list(keywords)
    bok_about = _bok_defined_terms(work)
    if bok_about:
        payload["about"] = bok_about
    if image:
        payload["image"] = image
    publisher_name = _publisher_name(work)
    if publisher_name:
        publisher: dict = {
            "@type": "Organization",
            "name": publisher_name,
        }
        if work.source and getattr(work.source, "homepage_url", None):
            publisher["url"] = work.source.homepage_url
        payload["publisher"] = publisher

    if work.source and getattr(work.source, "name", None):
        periodical: dict = {"@type": "Periodical", "name": str(work.source.name)}
        if getattr(work.source, "issn_l", None):
            periodical["issn"] = work.source.issn_l

        # Nest PublicationVolume / PublicationIssue when we have them so
        # consumers see the full citation structure; fall back to the flat
        # Periodical shape when neither is set.
        is_part_of = periodical
        if work.volume:
            is_part_of = {
                "@type": "PublicationVolume",
                "volumeNumber": work.volume,
                "isPartOf": periodical,
            }
        if work.issue:
            is_part_of = {
                "@type": "PublicationIssue",
                "issueNumber": work.issue,
                "isPartOf": is_part_of,
            }
        payload["isPartOf"] = is_part_of
    if work.first_page:
        payload["pageStart"] = work.first_page
    if work.last_page:
        payload["pageEnd"] = work.last_page
    if work.geometry and not work.geometry.empty:
        place: dict = {
            "@type": "Place",
            "geo": _format_geo_for_schema_org(work),
        }
        # Wikipedia/HTML geotagging tags work alongside the schema.org Place
        # — we publish placename + country/region here when known so JSON-LD
        # consumers (Google Knowledge Graph, etc.) and the meta-tag layer
        # share the same denormalized values.
        if getattr(work, "placename", None):
            place["name"] = work.placename
        country_codes = work.country_codes
        if country_codes:
            # schema.org addressCountry accepts a single value or a list.
            place["addressCountry"] = country_codes[0] if len(country_codes) == 1 else country_codes
        payload["spatialCoverage"] = place
    temporal_intervals = _temporal_iso_intervals(work)
    if temporal_intervals:
        payload["temporalCoverage"] = temporal_intervals[0] if len(temporal_intervals) == 1 else temporal_intervals
    payload["inLanguage"] = work.language or "en"
    return payload


def _format_geo_for_schema_org(work) -> dict:
    """Return a schema.org-compliant ``geo`` value for ``work.geometry``.

    Per https://schema.org/geo, a ``Place.geo`` must be a ``GeoCoordinates``
    (single point) or a ``GeoShape`` (polygon / line / box / circle).
    The previous implementation dumped the raw GeoJSON ``GeometryCollection``,
    which isn't a valid value and was ignored by structured-data consumers.

    Detection rule: when the geometry is a single ``Point`` (either bare or
    wrapped in a one-child ``GeometryCollection``) we emit ``GeoCoordinates``;
    otherwise we fall back to ``GeoShape`` with a ``box`` from the envelope.
    The ``box`` order is ``"south west north east"`` to match
    ``build_feed_page_meta``.
    """
    geom = work.geometry
    inner = geom
    # Unwrap a one-Point GeometryCollection so single-point works render as
    # GeoCoordinates rather than a degenerate zero-area box.
    if geom.geom_type == "GeometryCollection" and len(geom) == 1:
        inner = geom[0]
    if inner.geom_type == "Point":
        return {
            "@type": "GeoCoordinates",
            "latitude": round(inner.y, COORDINATE_PRECISION),
            "longitude": round(inner.x, COORDINATE_PRECISION),
        }
    west, south, east, north = geom.extent
    s, w, n, e = (round(v, COORDINATE_PRECISION) for v in (south, west, north, east))
    return {
        "@type": "GeoShape",
        "box": f"{s} {w} {n} {e}",
    }


def geo_meta_tags(work) -> list[dict]:
    """List of HTML geotagging ``<meta name=… content=…>`` dicts for ``work``.

    Emits the conventional tags from the Wikipedia *Geotagging — HTML pages*
    article and Yahoo's ICBM variant when a centroid can be computed:

    - ``geo.position``  → ``"lat;lon"`` (semicolon)
    - ``ICBM``          → ``"lat, lon"`` (comma + space)
    - ``geo.placename`` → human-readable placename, when ``Work.placename`` is set
    - ``geo.region``    → ISO 3166-1 alpha-2 code(s) from ``Work.countries``
      (comma-joined when the geometry spans multiple countries)

    Returns ``[]`` when the work has no geometry. The two coordinate-based
    tags use different separators by design — both forms are still consumed
    by long-tail crawlers and we incur no cost emitting both.
    """
    if not work.geometry or work.geometry.empty:
        return []
    center = work.get_center_coordinate()
    if not center:
        return []
    lon, lat = center
    lon = round(lon, COORDINATE_PRECISION)
    lat = round(lat, COORDINATE_PRECISION)
    tags: list[dict] = [
        {"name": "geo.position", "content": f"{lat};{lon}"},
        {"name": "ICBM", "content": f"{lat}, {lon}"},
    ]
    placename = getattr(work, "placename", None)
    if placename:
        tags.append({"name": "geo.placename", "content": placename})
    country_codes = work.country_codes
    if country_codes:
        tags.append({"name": "geo.region", "content": ", ".join(country_codes)})
    return tags


def _temporal_iso_intervals(work) -> list[str]:
    """Return a list of ISO 8601 interval strings for all temporal periods.
    Each entry: ``start/end``, ``start/..``, or ``../end``.
    Returns an empty list when both arrays are empty."""
    s_list = work.timeperiod_startdate or []
    e_list = work.timeperiod_enddate or []
    n = max(len(s_list), len(e_list), 0)
    intervals = []
    for i in range(n):
        s = (s_list[i] if i < len(s_list) else None) or None
        e = (e_list[i] if i < len(e_list) else None) or None
        if s or e:
            intervals.append(f"{s or '..'}/{e or '..'}")
    return intervals


def _format_temporal_iso(work) -> str | None:
    """Comma-separated ISO 8601 intervals — kept for backwards compat with
    any callers that expect a single string. The JSON-LD builder uses
    ``_temporal_iso_intervals`` directly to emit an array when needed."""
    intervals = _temporal_iso_intervals(work)
    return ", ".join(intervals) or None


def dc_coverage_tags(work) -> list[dict]:
    """Return one ``DC.temporal`` meta-tag dict per time period.

    Each entry follows the ISO 8601 interval convention already used for
    ``temporalCoverage`` in the schema.org JSON-LD payload, mirroring what
    the OAI-PMH harvester reads from source HTML (``DC.temporal`` /
    ``DC.PeriodOfTime``).  One tag per period lets consumers that parse
    repeated ``DC.temporal`` tags (e.g. Zotero's Dublin Core translator)
    ingest multi-period coverage without splitting on delimiters.
    """
    return [{"name": "DC.temporal", "content": iv} for iv in _temporal_iso_intervals(work)]


def _derive_pdf_url(work) -> str | None:
    """Return ``work.url`` when it confidently points at a PDF, else None.

    Emitting a non-PDF URL as ``citation_pdf_url`` causes Zotero to attach
    an HTML snapshot as if it were a PDF, so we err strict. (Note:
    ``Work.openalex_fulltext_origin`` is a *type* string from OpenAlex —
    e.g. "journal", "repository" — not a URL, so it can't be used here.)
    """
    if work.url and work.url.lower().endswith(".pdf"):
        return work.url
    return None


def citation_meta_tags(work, request) -> list[dict]:
    """List of Google Scholar ``citation_*`` tag dicts ``{name, content}``.
    The template renders these directly because django-meta has no model
    for Scholar tags. Repeating ``citation_author`` and ``citation_keywords``
    is intentional — Scholar expects one tag per item.
    """
    tags: list[dict] = []
    if work.title:
        tags.append({"name": "citation_title", "content": work.title})
    for author in _normalize_author_list(work):
        tags.append({"name": "citation_author", "content": author})
    if work.publicationDate:
        tags.append(
            {
                "name": "citation_publication_date",
                "content": work.publicationDate.isoformat(),
            }
        )
    if work.doi:
        tags.append({"name": "citation_doi", "content": work.doi})
    canonical = _abs(request, reverse("optimap:work-landing", args=[work.get_identifier()]))
    tags.append({"name": "citation_abstract_html_url", "content": canonical})
    if work.abstract:
        tags.append({"name": "citation_abstract", "content": work.abstract})
    if work.source and getattr(work.source, "name", None):
        tags.append({"name": "citation_journal_title", "content": str(work.source.name)})
    publisher = _publisher_name(work)
    if publisher:
        tags.append({"name": "citation_publisher", "content": publisher})
    if work.source and getattr(work.source, "issn_l", None):
        tags.append({"name": "citation_issn", "content": work.source.issn_l})
    if work.volume:
        tags.append({"name": "citation_volume", "content": work.volume})
    if work.issue:
        tags.append({"name": "citation_issue", "content": work.issue})
    if work.first_page:
        tags.append({"name": "citation_firstpage", "content": work.first_page})
    if work.last_page:
        tags.append({"name": "citation_lastpage", "content": work.last_page})
    keywords: list[str] = []
    if work.keywords:
        keywords.extend(k for k in work.keywords if k)
    if work.topics:
        keywords.extend(t for t in work.topics if t)
    for kw in keywords:
        tags.append({"name": "citation_keywords", "content": kw})
    tags.append({"name": "citation_language", "content": work.language or "en"})
    pdf_url = _derive_pdf_url(work)
    if pdf_url:
        tags.append({"name": "citation_pdf_url", "content": pdf_url})
    return tags


def coins_title(work) -> str | None:
    """Return the value for a COinS ``<span class="Z3988" title="…">`` for
    this work, or ``None`` when the work has no title.

    COinS encodes one OpenURL kev/mtx context object as a single URL-encoded
    query string. Zotero (and other reference managers) read it as a
    fallback when the richer Embedded Metadata translator finds nothing —
    or to enable multi-item save on list pages.
    """
    if not work.title:
        return None

    pairs: list[tuple[str, str]] = [
        ("ctx_ver", "Z39.88-2004"),
        ("rft_val_fmt", "info:ofi/fmt:kev:mtx:journal"),
        ("rft.genre", "article"),
        ("rft.atitle", work.title),
    ]
    for author in _normalize_author_list(work):
        pairs.append(("rft.au", author))
    if work.publicationDate:
        pairs.append(("rft.date", work.publicationDate.isoformat()))
    if work.source and getattr(work.source, "name", None):
        pairs.append(("rft.jtitle", str(work.source.name)))
    publisher = _publisher_name(work)
    if publisher:
        pairs.append(("rft.pub", publisher))
    if work.source and getattr(work.source, "issn_l", None):
        pairs.append(("rft.issn", work.source.issn_l))
    if work.volume:
        pairs.append(("rft.volume", work.volume))
    if work.issue:
        pairs.append(("rft.issue", work.issue))
    if work.first_page:
        pairs.append(("rft.spage", work.first_page))
    if work.last_page:
        pairs.append(("rft.epage", work.last_page))
    if work.doi:
        pairs.append(("rft_id", f"info:doi/{work.doi}"))

    return "&".join(f"{k}={quote(v, safe='')}" for k, v in pairs)


def build_homepage_meta(request) -> Meta:
    """``WebSite`` + ``SearchAction`` schema.org for the homepage."""
    canonical = _abs(request, reverse("optimap:main"))
    description = (
        "OPTIMAP is a geospatial discovery portal for research articles "
        "based on open metadata. Browse publications by region, time, and "
        "subject."
    )
    meta = Meta(
        request=request,
        title="OPTIMAP — geospatial discovery of research articles",
        description=description,
        keywords=["OPTIMAP", "geospatial search", "open access", "research articles", "KOMET"],
        url=canonical,
        image=_abs(request, _OG_FALLBACK_IMAGE),
        site_name="OPTIMAP",
        object_type="website",
        schemaorg_title="OPTIMAP",
        schemaorg_description=description,
    )
    meta.schema = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": "OPTIMAP",
        "url": canonical,
        "description": description,
        "potentialAction": {
            "@type": "SearchAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": _abs(request, "/?q={search_term_string}"),
            },
            "query-input": "required name=search_term_string",
        },
    }
    return meta


def build_feed_page_meta(
    request, *, region_name: str | None, region_bbox: Iterable[float] | None, page_url: str
) -> Meta:
    """``CollectionPage`` schema.org for region/feed landing pages."""
    canonical = _abs(request, page_url)
    title = f"{region_name} — OPTIMAP regional feed" if region_name else "OPTIMAP regions"
    description = (
        f"Recent research articles with geographic coverage in {region_name}."
        if region_name
        else "OPTIMAP regional pages and feeds."
    )
    meta = Meta(
        request=request,
        title=title,
        description=description,
        url=canonical,
        image=_abs(request, _OG_FALLBACK_IMAGE),
        site_name="OPTIMAP",
        object_type="website",
        schemaorg_title=title,
        schemaorg_description=description,
    )
    schema: dict = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": title,
        "url": canonical,
        "description": description,
    }
    if region_name and region_bbox:
        west, south, east, north = region_bbox
        schema["about"] = {
            "@type": "Place",
            "name": region_name,
            "geo": {
                "@type": "GeoShape",
                "box": f"{south} {west} {north} {east}",
            },
        }
    elif region_name:
        schema["about"] = {"@type": "Place", "name": region_name}
    meta.schema = schema
    return meta


def build_facet_page_meta(request, *, title: str, description: str, page_url: str, about: dict | None = None) -> Meta:
    """``CollectionPage`` schema.org for faceted permalink pages.

    Shared by the source (``/in/``), place (``/at/``), year (``/during/``) and
    topic (``/on/``) landing pages so every indexed page gets a canonical URL,
    an Open Graph card, and a schema.org ``CollectionPage`` block (#29).
    """
    canonical = _abs(request, page_url)
    meta = Meta(
        request=request,
        title=title,
        description=description,
        url=canonical,
        image=_abs(request, _OG_FALLBACK_IMAGE),
        site_name="OPTIMAP",
        object_type="website",
        schemaorg_title=title,
        schemaorg_description=description,
    )
    schema: dict = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": title,
        "url": canonical,
        "description": description,
    }
    if about:
        schema["about"] = about
    meta.schema = schema
    return meta


def _bok_defined_terms(work) -> list[dict]:
    """Render `Work.bok_concepts` as schema.org `DefinedTerm` entries.

    Returns an empty list when no concepts are tagged or the snapshot is
    unreachable. Orphan codes (no longer in the cached snapshot) are
    rendered with `termCode` only — no `url` — and a name equal to the code.
    """
    codes = getattr(work, "bok_concepts", None) or []
    if not codes:
        return []
    try:
        from works.bok import client as bok_client

        resolved = bok_client.resolve(codes)
    except Exception:
        return []

    term_set = {
        "@type": "DefinedTermSet",
        "name": "EO4GEO Body of Knowledge (GeoSpaceBoK)",
        "url": "https://geospacebok.eu",
    }
    out: list[dict] = []
    for c in resolved:
        term: dict = {
            "@type": "DefinedTerm",
            "termCode": c["code"],
            "name": c.get("name") or c["code"],
            "inDefinedTermSet": term_set,
        }
        if c.get("uri"):
            term["url"] = c["uri"]
        if c.get("description"):
            term["description"] = c["description"]
        out.append(term)
    return out
