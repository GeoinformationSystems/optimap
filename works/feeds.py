# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Improved feed implementation for OPTIMAP with caching, validation fixes, and regional feeds.
"""

import logging
import urllib.parse
from datetime import datetime

from django.conf import settings
from django.contrib.syndication.views import Feed
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.feedgenerator import Atom1Feed, Rss201rev2Feed

from .models import Collection, Country, GlobalRegion, Source, Work
from .utils.geometry import COORDINATE_PRECISION

logger = logging.getLogger(__name__)


class ValidGeoRssFeed(Rss201rev2Feed):
    """RSS 2.0 feed with proper GeoRSS namespace and validation fixes."""

    def __init__(self, *args, **kwargs):
        self.feed_type_variant = kwargs.pop("feed_type_variant", "georss")
        super().__init__(*args, **kwargs)

    def rss_attributes(self):
        """Add proper namespace declarations."""
        return {
            "version": self._version,
            "xmlns:atom": "http://www.w3.org/2005/Atom",
            "xmlns:georss": "http://www.georss.org/georss",
            "xmlns:gml": "http://www.opengis.net/gml",
        }

    def add_root_elements(self, handler):
        """Add required RSS elements."""
        super().add_root_elements(handler)

        # Add atom:link for feed self-reference (required for validation)
        if self.feed.get("feed_url"):
            handler.addQuickElement(
                "atom:link", None, {"href": self.feed["feed_url"], "rel": "self", "type": "application/rss+xml"}
            )

    def add_item_elements(self, handler, item):
        """Add item elements with proper GeoRSS formatting."""
        super().add_item_elements(handler, item)

        # Add GeoRSS elements
        if self.feed_type_variant == "georss":
            if "georss_point" in item:
                handler.addQuickElement("georss:point", item["georss_point"])
            if "georss_polygon" in item:
                handler.addQuickElement("georss:polygon", item["georss_polygon"])
            if "georss_line" in item:
                handler.addQuickElement("georss:line", item["georss_line"])

        # Add source/publishing venue if available
        if "source_name" in item and item["source_name"]:
            handler.startElement("source", {})
            handler.addQuickElement("title", item["source_name"])
            if "source_url" in item and item["source_url"]:
                handler.addQuickElement("url", item["source_url"])
            handler.endElement("source")


class ValidGeoAtomFeed(Atom1Feed):
    """Atom 1.0 feed with proper GeoRSS namespace and validation fixes."""

    def root_attributes(self):
        """Add proper namespace declarations."""
        attrs = super().root_attributes()
        attrs["xmlns:georss"] = "http://www.georss.org/georss"
        attrs["xmlns:gml"] = "http://www.opengis.net/gml"
        return attrs

    def add_root_elements(self, handler):
        """Add root elements with proper self-reference link."""
        super().add_root_elements(handler)
        # The self link is already added by Django's Atom1Feed
        # It uses feed['feed_url'] which we set in get_feed()

    def add_item_elements(self, handler, item):
        """Add item elements with proper GeoRSS formatting."""
        super().add_item_elements(handler, item)

        # Add GeoRSS elements
        if "georss_point" in item:
            handler.addQuickElement("georss:point", item["georss_point"])
        if "georss_polygon" in item:
            handler.addQuickElement("georss:polygon", item["georss_polygon"])
        if "georss_line" in item:
            handler.addQuickElement("georss:line", item["georss_line"])

        # Add source/publishing venue if available
        if "source_name" in item and item["source_name"]:
            handler.startElement("source", {})
            handler.addQuickElement("title", item["source_name"])
            if "source_url" in item and item["source_url"]:
                handler.addQuickElement("link", "", {"href": item["source_url"], "rel": "alternate"})
            handler.endElement("source")


def _format_georss_geometry(geometry):
    """
    Format Django geometry objects into GeoRSS elements.

    Args:
        geometry: Django GEOSGeometry object

    Returns:
        list of tuples: (element_name, element_value) pairs
    """
    georss_data = []

    if geometry.geom_type == "Point":
        lat = round(geometry.y, COORDINATE_PRECISION)
        lon = round(geometry.x, COORDINATE_PRECISION)
        georss_data.append(("georss_point", f"{lat} {lon}"))

    elif geometry.geom_type == "LineString":
        coords = " ".join(
            f"{round(pt[1], COORDINATE_PRECISION)} {round(pt[0], COORDINATE_PRECISION)}" for pt in geometry.coords
        )
        georss_data.append(("georss_line", coords))

    elif geometry.geom_type == "Polygon":
        coords = " ".join(
            f"{round(pt[1], COORDINATE_PRECISION)} {round(pt[0], COORDINATE_PRECISION)}" for pt in geometry.coords[0]
        )
        georss_data.append(("georss_polygon", coords))

    elif geometry.geom_type == "GeometryCollection":
        # For geometry collections, take the first non-empty geometry
        for geom in geometry:
            georss_data.extend(_format_georss_geometry(geom))
            if georss_data:  # Stop after first successful geometry
                break

    return georss_data


def normalize_region_slug(slug):
    """
    Normalize a region slug to lowercase with hyphens.

    Args:
        slug: Input slug (may contain underscores, spaces, mixed case)

    Returns:
        str: Normalized slug (lowercase, hyphens instead of spaces/underscores)
    """
    decoded = urllib.parse.unquote(slug).strip().lower()

    # Remove .geojson extension if present
    if decoded.endswith(".geojson"):
        decoded = decoded[: -len(".geojson")]

    # Replace underscores and spaces with hyphens
    normalized = decoded.replace("_", "-").replace(" ", "-")

    return normalized


def get_region_from_slug(slug):
    """
    Get a GlobalRegion from a slug by comparing with region's get_slug() method.

    Args:
        slug: URL slug for the region

    Returns:
        GlobalRegion or None
    """
    normalized = normalize_region_slug(slug)

    # Get all regions and find the one whose slug matches
    # This is more efficient than trying multiple name variations
    for region in GlobalRegion.objects.all():
        if region.get_slug() == normalized:
            logger.debug("Found region '%s' for slug '%s'", region.name, slug)
            return region

    logger.warning("No region found for slug '%s'", slug)
    return None


class BaseCachedGeoFeed(Feed):
    """
    Base class for geo feeds with caching support.

    Implements caching logic with ?now parameter to force refresh.
    """

    feed_type_variant = "georss"

    def feed_extra_kwargs(self, obj):
        """Add extra kwargs for the feed itself."""
        return {}

    def author_name(self):
        """Return feed author name."""
        return "OPTIMAP"

    def author_email(self):
        """Return feed author email."""
        return "noreply@optimap.science"

    def author_link(self):
        """Return feed author link."""
        return settings.BASE_URL

    def __call__(self, request, *args, **kwargs):
        """
        Override __call__ to implement caching.

        Cache feeds for FEED_CACHE_HOURS unless ?now parameter is present.
        """
        # Check for ?now parameter to force refresh
        force_refresh = request.GET.get("now") is not None

        # Build cache key
        cache_key = self._get_cache_key(request, *args, **kwargs)

        if not force_refresh:
            # Try to get cached response
            cached_response = cache.get(cache_key)
            if cached_response:
                logger.debug("Serving cached feed: %s", cache_key)
                return cached_response

        # Generate fresh feed
        logger.debug("Generating fresh feed: %s", cache_key)
        response = super().__call__(request, *args, **kwargs)

        # Cache the response
        cache_hours = getattr(settings, "FEED_CACHE_HOURS", 24)
        cache.set(cache_key, response, timeout=cache_hours * 3600)

        return response

    def _get_cache_key(self, request, *args, **kwargs):
        """Generate cache key for this feed."""
        path = request.path
        variant = self.feed_type_variant
        return f"feed:{variant}:{path}"

    def get_feed(self, obj, request):
        """Set up the correct feed type."""
        if self.feed_type_variant == "atom":
            self.feed_type = ValidGeoAtomFeed
        else:
            self.feed_type = lambda *args, **kwargs: ValidGeoRssFeed(
                *args, **kwargs, feed_type_variant=self.feed_type_variant
            )

        feed = super().get_feed(obj, request)

        # Add feed_url for atom:link self-reference
        feed.feed["feed_url"] = request.build_absolute_uri(request.path)

        return feed

    def item_title(self, item):
        """Return item title."""
        return item.title

    def item_description(self, item):
        """Return item description."""
        return item.abstract or "No abstract available."

    def item_link(self, item):
        """Return item link - prefer permalink if available."""
        permalink = item.permalink()
        if permalink:
            return permalink
        return item.url or ""

    def item_pubdate(self, item):
        """Return item work date."""
        if item.publicationDate:
            return datetime.combine(item.publicationDate, datetime.min.time())
        return item.creationDate

    def item_updateddate(self, item):
        """Return item updated date (required for Atom feeds)."""
        # Use lastUpdate if available, otherwise fall back to creation date
        if hasattr(item, "lastUpdate") and item.lastUpdate:
            return item.lastUpdate
        return item.creationDate

    def item_author_name(self, item):
        """Return item author name (required for Atom feeds)."""
        # Try to extract authors from OpenAlex field
        authors = self._extract_authors_from_item(item)
        if authors:
            # Return all authors if 10 or fewer, otherwise use "et al."
            if len(authors) == 1:
                return authors[0]
            elif len(authors) <= 10:
                return ", ".join(authors)
            else:
                return f"{authors[0]} et al."

        # If no authors found, return None (publishing venue will be in source element)
        return None

    def _extract_authors_from_item(self, item):
        """
        Extract author names from work item.

        Tries multiple sources:
        1. authors field (primary)
        2. OpenAlex match info (fallback for older data)

        Returns:
            list: Author names, or empty list if none found
        """
        authors = []

        # Primary: Try authors field
        if hasattr(item, "authors") and item.authors:
            authors = [a for a in item.authors if a]
            if authors:
                return authors

        # Fallback: Try OpenAlex match info (for older works)
        if hasattr(item, "openalex_match_info") and item.openalex_match_info:
            if isinstance(item.openalex_match_info, list) and len(item.openalex_match_info) > 0:
                first_match = item.openalex_match_info[0]
                if isinstance(first_match, dict) and "authors" in first_match:
                    match_authors = first_match["authors"]
                    if match_authors:
                        # Filter out None values
                        authors = [a for a in match_authors if a]

        return authors

    def item_author_email(self, item):
        """Return item author email (optional for Atom feeds)."""
        # Return None to avoid validation issues with placeholder emails
        return None

    def item_author_link(self, item):
        """Return item author link (optional for Atom feeds)."""
        # Return the main site URL
        return "/"

    def item_extra_kwargs(self, item):
        """Add GeoRSS elements and other metadata to feed items."""
        extra = {}

        # Add GeoRSS geometry elements
        if item.geometry and not item.geometry.empty:
            geometries = _format_georss_geometry(item.geometry)
            for key, value in geometries:
                extra[key] = value

        # Add source/publishing venue information
        if hasattr(item, "source") and item.source:
            extra["source_name"] = item.source.name
            if hasattr(item.source, "homepage_url") and item.source.homepage_url:
                extra["source_url"] = item.source.homepage_url

        return extra

    def item_categories(self, item):
        """Return item categories (keywords and topics)."""
        categories = []

        # Add keywords
        if hasattr(item, "keywords") and item.keywords:
            categories.extend([kw for kw in item.keywords if kw])

        # Add topics (limit to first 3 to avoid clutter)
        if hasattr(item, "topics") and item.topics:
            topics = [t for t in item.topics if t][:3]
            categories.extend(topics)

        return categories if categories else None


class GlobalGeoFeed(BaseCachedGeoFeed):
    """Global feed containing all published works."""

    def __init__(self, feed_type_variant="georss"):
        self.feed_type_variant = feed_type_variant
        super().__init__()

    def title(self):
        """Return feed title."""
        return "OPTIMAP - Latest works"

    def link(self):
        """Return feed link."""
        return "/"

    def description(self):
        """Return feed description."""
        return "Latest research works with geographic metadata from OPTIMAP."

    def items(self):
        """Return feed items."""
        return (
            Work.objects.filter(
                status="p",
                geometry__isnull=False,
            )
            .exclude(url__isnull=True)
            .exclude(url__exact="")
            .order_by("-creationDate")[: settings.FEED_MAX_ITEMS]
        )


class RegionalGeoFeed(BaseCachedGeoFeed):
    """Feed filtered by global region (continent or ocean)."""

    def __init__(self, feed_type_variant="georss"):
        self.feed_type_variant = feed_type_variant
        super().__init__()

    def get_object(self, request, **kwargs):
        """Get the region object from the slug."""
        # Accept both continent_slug and ocean_slug from URL patterns
        region_slug = kwargs.get("continent_slug") or kwargs.get("ocean_slug")
        if not region_slug:
            raise Http404("No region slug provided")

        region = get_region_from_slug(region_slug)
        if region is None:
            raise Http404(f"Region not found: {region_slug}")
        return region

    def title(self, obj):
        """Return feed title with region name."""
        region_type = obj.get_region_type_display()
        return f"OPTIMAP - {obj.name} ({region_type}) - Latest works"

    def link(self, obj):
        """Return feed link."""
        return "/"

    def description(self, obj):
        """Return feed description with region name."""
        region_type = obj.get_region_type_display()
        return f"Latest research works with geographic metadata from {obj.name} ({region_type}) on OPTIMAP."

    def items(self, obj):
        """Return feed items filtered by region geometry."""
        # Use bbox overlap first for performance
        candidates = (
            Work.objects.filter(
                status="p",
                geometry__isnull=False,
                geometry__bboverlaps=obj.geom,
            )
            .exclude(url__isnull=True)
            .exclude(url__exact="")
            .order_by("-creationDate")
        )

        # Prepare geometry for faster intersection checks
        prepared_geom = obj.geom.prepared

        # Filter by actual intersection and limit
        filtered = [work for work in candidates if prepared_geom.intersects(work.geometry)][: settings.FEED_MAX_ITEMS]

        logger.debug(
            "Region feed '%s': %d candidates, %d after intersection with a limit of %d",
            obj.name,
            candidates.count(),
            len(filtered),
            settings.FEED_MAX_ITEMS,
        )

        return filtered


class CollectionGeoFeed(BaseCachedGeoFeed):
    """Feed filtered by curated collection."""

    def __init__(self, feed_type_variant="georss"):
        self.feed_type_variant = feed_type_variant
        super().__init__()

    def get_object(self, request, collection_slug):
        return get_object_or_404(Collection, identifier=collection_slug, is_published=True)

    def title(self, obj):
        return f"OPTIMAP – {obj.name}"

    def link(self, obj):
        return obj.get_absolute_url()

    def description(self, obj):
        return obj.description or f"Latest works in collection '{obj.name}'"

    def items(self, obj):
        return (
            Work.objects.filter(collections=obj, status="p")
            .exclude(geometry__isnull=True)
            .order_by("-creationDate")[: settings.FEED_MAX_ITEMS]
        )


class CountryGeoFeed(BaseCachedGeoFeed):
    """Feed filtered by country (issue #52)."""

    def __init__(self, feed_type_variant="georss"):
        self.feed_type_variant = feed_type_variant
        super().__init__()

    def __call__(self, request, *args, **kwargs):
        country_slug = kwargs.get("country_slug", "")
        if len(country_slug) == 2 and country_slug.isalpha():
            country = Country.objects.real().filter(iso_code=country_slug.upper()).first()
            if country is not None:
                url_name = (
                    "optimap:api-country-atom" if self.feed_type_variant == "atom" else "optimap:api-country-georss"
                )
                url = reverse(url_name, kwargs={"country_slug": country.slug})
                qs = request.GET.urlencode()
                return redirect(f"{url}?{qs}" if qs else url, permanent=True)
        return super().__call__(request, *args, **kwargs)

    def get_object(self, request, country_slug):
        return get_object_or_404(Country.objects.real(), slug=country_slug)

    def title(self, obj):
        return f"OPTIMAP – {obj.name}"

    def link(self, obj):
        return obj.get_absolute_url()

    def description(self, obj):
        return f"Latest works in {obj.name}"

    def items(self, obj):
        return (
            Work.objects.filter(countries=obj, status="p")
            .exclude(geometry__isnull=True)
            .exclude(url__isnull=True)
            .exclude(url__exact="")
            .order_by("-creationDate")[: settings.FEED_MAX_ITEMS]
        )


class SourceGeoFeed(BaseCachedGeoFeed):
    """Feed of published works from a single source (issue #253)."""

    def __init__(self, feed_type_variant="georss"):
        self.feed_type_variant = feed_type_variant
        super().__init__()

    def get_object(self, request, source_slug):
        return get_object_or_404(Source, slug=source_slug)

    def title(self, obj):
        return f"OPTIMAP – {obj.name}"

    def link(self, obj):
        return obj.get_absolute_url()

    def description(self, obj):
        return f"Latest works from source '{obj.name}'"

    def items(self, obj):
        return (
            Work.objects.filter(source=obj, status="p")
            .exclude(geometry__isnull=True)
            .order_by("-creationDate")[: settings.FEED_MAX_ITEMS]
        )
