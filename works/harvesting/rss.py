# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""RSS / Atom feed harvester."""

import logging
import re

from bs4 import BeautifulSoup
from django.contrib.gis.geos import GeometryCollection
from django.utils import timezone

from works.models import HarvestingEvent, Source

from .common import (
    HarvestStats,
    HarvestWarningCollector,
    _backfill_empty_doi,
    _find_existing_work,
    _save_or_update_work,
    complete_harvest,
    fail_harvest,
    get_or_create_admin_command_user,
    log_work_enriched,
    log_work_failed,
    log_work_retrieved,
    log_work_skip,
    parse_publication_date,
    render_harvest_email,
    resolve_user,
    send_harvest_email,
    start_harvesting_event,
)
from .openalex import build_openalex_fields, link_openalex_authors

logger = logging.getLogger(__name__)
DOI_REGEX = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def parse_rss_feed_and_save_publications(
    feed_url, event: "HarvestingEvent", max_records=None, warning_collector=None, update_existing=False, stats=None
):
    """
    Parse RSS/Atom feed and save publications.

    Args:
        feed_url: URL of the RSS/Atom feed
        event: HarvestingEvent instance
        max_records: Maximum number of records to process (optional)
        warning_collector: HarvestWarningCollector instance (optional)
        stats: HarvestStats accumulator (optional). If provided, the parser
            increments .created / .updated / .skipped_* on it; the harvester
            then reads .updated to populate ``HarvestingEvent.records_updated``.

    Returns:
        tuple: (processed_count, saved_count)
    """
    import feedparser

    source = event.source
    logger.info("Starting RSS/Atom feed parsing for source: %s", source.name)
    if stats is None:
        stats = HarvestStats()

    try:
        feed = feedparser.parse(feed_url)

        if not feed or not hasattr(feed, "entries"):
            logger.error("Failed to parse RSS feed: %s", feed_url)
            return 0, 0

        entries = feed.entries
        logger.info("Found %d entries in RSS feed", len(entries))

        if not entries:
            logger.warning("No entries found in RSS feed!")
            return 0, 0

        if max_records:
            entries = entries[:max_records]
            logger.info("Limited to first %d records", max_records)

        processed_count = 0
        saved_count = 0

        total_entries = len(entries)
        log_interval = 20 if total_entries <= 100 else 50

        for entry in entries:
            try:
                processed_count += 1
                if processed_count % log_interval == 0:
                    logger.info("Processed %d of %d records", processed_count, total_entries)

                title = entry.get("title", "").strip()
                link = entry.get("link", entry.get("id", "")).strip()

                doi = None
                if "prism_doi" in entry:
                    doi = entry.prism_doi.strip()
                elif "dc_identifier" in entry and "doi" in entry.dc_identifier.lower():
                    doi_match = DOI_REGEX.search(entry.dc_identifier)
                    if doi_match:
                        doi = doi_match.group(0)

                published_date = None
                date_str = entry.get("updated", entry.get("published", entry.get("dc_date")))
                if date_str:
                    if hasattr(date_str, "strftime"):
                        published_date = date_str.strftime("%Y-%m-%d")
                    else:
                        published_date = parse_publication_date(str(date_str))

                abstract = ""
                if "summary" in entry:
                    abstract = BeautifulSoup(entry.summary, "html.parser").get_text()
                elif "content" in entry and entry.content:
                    abstract = BeautifulSoup(entry.content[0].get("value", ""), "html.parser").get_text()

                if not title:
                    logger.warning("Skipping entry with no title: %s", link)
                    continue
                if not link:
                    logger.warning("Skipping entry '%s' with no URL", title[:50])
                    continue

                logger.debug("Processing work: %s", title[:50])

                # Early dedup: skip OpenAlex for records already in the database.
                _early_existing = _find_existing_work(doi=doi, url=link)
                if _early_existing is not None:
                    if doi and not _early_existing.doi:
                        _backfill_empty_doi(_early_existing, doi, event)
                    _cross_source = _early_existing.source_id != source.id
                    if _cross_source or not update_existing:
                        action = "skipped_cross_source" if _cross_source else "skipped_same_source"
                        stats.record(action)
                        log_work_skip(event, title, _cross_source)
                        continue

                log_work_retrieved(event, title)

                author = None
                authors_list = []
                if "author" in entry:
                    author = entry.author
                    authors_list = [a.strip() for a in author.replace(";", ",").split(",") if a.strip()]
                elif "dc_creator" in entry:
                    author = entry.dc_creator
                    authors_list = [a.strip() for a in author.replace(";", ",").split(",") if a.strip()]
                elif "authors" in entry:
                    authors_list = [a.get("name", "").strip() for a in entry.authors if a.get("name")]
                    author = ", ".join(authors_list) if authors_list else None

                keywords_list = []
                if "tags" in entry:
                    keywords_list = [tag.get("term", "").strip() for tag in entry.tags if tag.get("term")]
                elif "categories" in entry:
                    if isinstance(entry.categories, list):
                        keywords_list = [
                            cat.get("term", "").strip()
                            for cat in entry.categories
                            if isinstance(cat, dict) and cat.get("term")
                        ]
                elif "dc_subject" in entry:
                    subject = entry.dc_subject
                    keywords_list = [k.strip() for k in subject.replace(";", ",").split(",") if k.strip()]

                existing_metadata = {}
                if authors_list:
                    existing_metadata["authors"] = authors_list
                if keywords_list:
                    existing_metadata["keywords"] = keywords_list

                openalex_fields, metadata_provenance = build_openalex_fields(
                    title=title, doi=doi, author=author, existing_metadata=existing_metadata
                )
                log_work_enriched(event, title, openalex_fields)

                admin_user = get_or_create_admin_command_user()

                provenance = {
                    "harvest": {
                        "harvester": "harvest_rss_endpoint",
                        "source_url": feed_url,
                        "source_type": source.source_type,
                        "source_name": source.name,
                        "harvested_at": timezone.now().isoformat(),
                        "harvesting_event_id": event.id,
                    },
                    "metadata_sources": dict(metadata_provenance or {}),
                }

                work_kwargs = dict(
                    title=title,
                    doi=doi,
                    url=link,
                    abstract=abstract[:5000] if abstract else None,
                    publicationDate=published_date,
                    source=source,
                    job=event,
                    timeperiod_startdate=[],
                    timeperiod_enddate=[],
                    geometry=GeometryCollection(),  # No spatial data from RSS typically
                    provenance=provenance,
                    created_by=admin_user,
                    **openalex_fields,
                )
                work, action = _save_or_update_work(
                    work_kwargs,
                    source,
                    event,
                    update_existing=update_existing,
                )
                stats.record(action)
                if action in ("created", "updated"):
                    link_openalex_authors(work, openalex_fields)
                    if source and source.collection_id:
                        work.collections.add(source.collection_id)
                if action == "created":
                    saved_count += 1
                    logger.debug("Saved work: %s", title[:50])
                elif action == "updated":
                    logger.debug("Updated work: %s", title[:50])

            except Exception as e:
                logger.error("Failed to process entry '%s': %s", entry.get("title", "Unknown")[:50], str(e))
                log_work_failed(event, entry.get("title", "Unknown"), e)
                continue

        logger.info(
            "RSS feed parsing completed for source %s: processed %d entries, created %d, updated %d, skipped %d",
            source.name,
            processed_count,
            stats.created,
            stats.updated,
            stats.skipped_same_source + stats.skipped_cross_source,
        )
        return processed_count, saved_count

    except Exception as e:
        logger.error("Failed to parse RSS feed %s: %s", feed_url, str(e))
        return 0, 0


def harvest_rss_endpoint(
    source_id, user=None, max_records=None, update_existing=False, event_id=None, trigger_source=None
):
    """Harvest publications from an RSS/Atom feed."""
    user = resolve_user(user)
    source = Source.objects.get(id=source_id)
    event = start_harvesting_event(source, event_id, trigger_source, max_records)
    if event is None:
        return

    warning_collector = HarvestWarningCollector()
    warning_collector.setLevel(logging.INFO)
    logger.addHandler(warning_collector)

    try:
        feed_url = source.url_field
        logger.info("Fetching from RSS feed: %s", feed_url)
        event.append_log(f"Fetching RSS/Atom feed: {feed_url}")

        stats = HarvestStats()
        parse_rss_feed_and_save_publications(
            feed_url,
            event,
            max_records=max_records,
            warning_collector=warning_collector,
            update_existing=update_existing,
            stats=stats,
        )
        event.append_log(f"Parsed feed: {stats.created} created, {stats.updated} updated, {stats.skipped} skipped.")

        spatial_count, temporal_count = complete_harvest(event, stats, warning_collector)
        new_count = stats.created
        updated_count = stats.updated

        subject, body = render_harvest_email(
            "email/harvest_success.en.txt",
            {
                "subject_prefix": "RSS Feed ",
                "source_label": source.name,
                "detail_header": "RSS/Atom feed harvesting job details:",
                "source_name": source.name,
                "source_url": source.url_field,
                "url_label": "Feed URL",
                "collection_label": None,
                "records_added_label": "Number of added articles",
                "records_added": new_count,
                "records_updated_label": "Number of updated articles",
                "records_updated": updated_count,
                "spatial_label": "Number of articles with spatial metadata",
                "spatial_count": spatial_count,
                "temporal_label": "Number of articles with temporal metadata",
                "temporal_count": temporal_count,
                "event_started": event.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                "event_completed": event.completed_at.strftime("%Y-%m-%d %H:%M:%S"),
                "warning_summary": warning_collector.get_summary(),
                "resolved_prefix": None,
                "container_title_filters": None,
                "openalex_source_id": None,
                "records_seen": None,
                "records_processed": None,
            },
            event=event,
        )
        send_harvest_email(user, subject, body)

    except Exception as e:
        logger.error("RSS feed harvesting failed for source %s: %s", source.url_field, str(e))
        fail_harvest(event, e, warning_collector)
        subject, body = render_harvest_email(
            "email/harvest_failure.en.txt",
            {
                "subject_prefix": "RSS Feed ",
                "source_label": source.name,
                "source_type_label": "RSS/Atom",
                "source_name": source.name,
                "source_url": source.url_field,
                "collection_label": None,
                "resolved_prefix": None,
                "event_started": None,
                "event_failed": None,
                "error": str(e),
                "warning_summary": warning_collector.get_summary() if warning_collector.has_issues() else "",
            },
        )
        send_harvest_email(user, subject, body, fail_silently=True)
    finally:
        logger.removeHandler(warning_collector)
