# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""OPTIMAP harvesting package.

One module per source type — `oai`, `rss`, `crossref`, `mountain_wetlands` —
plus shared helpers in `common`, `sessions`, `metadata_html`, and `openalex`.

The public entry-point functions are re-exported from `works.tasks` so
existing dotted-path Django-Q schedules (`works.tasks.harvest_oai_endpoint`
etc.) and test imports/patches keep resolving without migration.
"""

from .common import (
    HarvestStats,
    HarvestWarningCollector,
    _save_or_update_work,
    complete_harvest,
    fail_harvest,
    get_or_create_admin_command_user,
    log_work_enriched,
    log_work_failed,
    log_work_outcome,
    log_work_retrieved,
    log_work_skip,
    parse_publication_date,
    resolve_user,
    send_harvest_email,
)
from .crossref import (
    fetch_copernicus_abstract,
    harvest_crossref_prefix,
    parse_crossref_response_and_save_works,
)
from .metadata_html import (
    extract_geometry_from_html,
    extract_timeperiod_from_html,
)
from .mountain_wetlands import (
    harvest_mountain_wetlands,
    parse_mountain_wetlands_response_and_save_works,
)
from .oai import (
    harvest_oai_endpoint,
    parse_oai_xml_and_save_works,
)
from .openaire import (
    build_openaire_fields,
    enrich_event_from_openaire,
    enrich_work_from_openaire,
    fetch_openaire_record,
    get_openaire_access_token,
)
from .openalex import build_openalex_fields, openalex_enrichment_note
from .rss import (
    harvest_rss_endpoint,
    parse_rss_feed_and_save_publications,
)

__all__ = [
    # common
    "HarvestStats",
    "HarvestWarningCollector",
    "_save_or_update_work",
    "complete_harvest",
    "fail_harvest",
    "get_or_create_admin_command_user",
    "log_work_enriched",
    "log_work_failed",
    "log_work_outcome",
    "log_work_retrieved",
    "log_work_skip",
    "parse_publication_date",
    "resolve_user",
    "send_harvest_email",
    # metadata_html
    "extract_geometry_from_html",
    "extract_timeperiod_from_html",
    # openalex
    "build_openalex_fields",
    "openalex_enrichment_note",
    # openaire
    "build_openaire_fields",
    "enrich_event_from_openaire",
    "enrich_work_from_openaire",
    "fetch_openaire_record",
    "get_openaire_access_token",
    # oai
    "harvest_oai_endpoint",
    "parse_oai_xml_and_save_works",
    # rss
    "harvest_rss_endpoint",
    "parse_rss_feed_and_save_publications",
    # crossref
    "fetch_copernicus_abstract",
    "harvest_crossref_prefix",
    "parse_crossref_response_and_save_works",
    # mountain_wetlands
    "harvest_mountain_wetlands",
    "parse_mountain_wetlands_response_and_save_works",
]
