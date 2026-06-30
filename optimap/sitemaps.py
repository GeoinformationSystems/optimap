# SPDX-FileCopyrightText: 2023 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

from django.contrib.sitemaps import Sitemap
from django.db.models import Count, Q
from django.urls import reverse

from works.models import Author, Collection, Country, GlobalRegion, Source, Work


class WorksSitemap(Sitemap):  # based on django.contrib.sitemaps.GenericSitemap
    priority = 0.5
    changefreq = "weekly"
    queryset = Work.objects.all().filter(status="p")
    protocol = None

    def items(self):
        items = self.queryset.filter()
        # items.count()
        return items

    def location(self, item):
        """Return the URL path for a work (without domain)."""
        return reverse("optimap:work-landing", args=[item.get_identifier()])

    def lastmod(self, item):
        """Return the last modification date of the work."""
        return item.lastUpdate


class StaticViewSitemap(Sitemap):
    priority = 0.5
    changefreq = "monthly"

    def items(self):
        return [
            "main",  # Home page (/)
            "about",  # About page (/about/)
            "accessibility",  # Accessibility statement (/accessibility/)
            "browse",  # Facet directory (/browse/)
            "countries",  # Countries overview (/countries/)
            "at-index",  # Places index (/at/)
            "in-index",  # Sources index (/in/)
            "collections",  # Collections index (/collections/)
            "contribute",  # Contribute page (/contribute/)
            "data",  # Data download page (/data/)
            "feeds",  # RSS/Atom feeds listing (/feeds/)
            "geoextent",  # Geoextent extraction tool (/geoextent/)
            "privacy",  # Privacy policy (/privacy/)
            "redoc",  # API schema UI (/api/schema/ui/)
            "sitemap-page",  # Human-readable sitemap (/pages/)
            "works",  # Works listing (/works/)
        ]

    def location(self, item):
        return reverse(f"optimap:{item}")


class FeedsSitemap(Sitemap):
    """Sitemap for global regional feeds (continents and oceans)."""

    priority = 0.6
    changefreq = "daily"

    def items(self):
        """Return all GlobalRegion objects (continents and oceans)."""
        return GlobalRegion.objects.all().order_by("region_type", "name")

    def location(self, obj):
        """Return the feed page URL for each region."""
        return obj.get_absolute_url()

    def lastmod(self, obj):
        """Return the last modification date."""
        return obj.last_loaded


class CollectionsSitemap(Sitemap):
    """Sitemap for the curated /collections/<identifier>/ pages.

    Only published collections are exposed — unpublished ones are admin-only
    and must not leak via sitemaps.
    """

    priority = 0.6
    changefreq = "weekly"

    def items(self):
        return Collection.objects.filter(is_published=True).order_by("name")

    def location(self, obj):
        return obj.get_absolute_url()

    def lastmod(self, obj):
        return obj.updated_at


class CollectionFeedsSitemap(Sitemap):
    """Sitemap for collection GeoRSS and Atom feed URLs (#248)."""

    priority = 0.6
    changefreq = "daily"

    def items(self):
        return [
            (c, fmt) for c in Collection.objects.filter(is_published=True).order_by("name") for fmt in ("rss", "atom")
        ]

    def location(self, item):
        collection, fmt = item
        name = "api-collection-georss" if fmt == "rss" else "api-collection-atom"
        return reverse(f"optimap:{name}", kwargs={"collection_slug": collection.identifier})

    def lastmod(self, item):
        return item[0].updated_at


class CollectionDownloadsSitemap(Sitemap):
    """Sitemap for collection download endpoints (#217)."""

    priority = 0.5
    changefreq = "weekly"

    def items(self):
        return [
            (c, fmt)
            for c in Collection.objects.filter(is_published=True).order_by("name")
            for fmt in ("geojson", "gpkg", "csv")
        ]

    def location(self, item):
        collection, fmt = item
        return reverse(
            f"optimap:download-collection-{fmt}",
            kwargs={"collection_slug": collection.identifier},
        )

    def lastmod(self, item):
        return item[0].updated_at


# --- Faceted permalink pages (#29) + source landing pages (#253) ------------


class CountrySitemap(Sitemap):
    """Sitemap for /at/<country>/ pages with at least one published work."""

    priority = 0.5
    changefreq = "weekly"

    def items(self):
        return Country.objects.filter(works__status="p").distinct().order_by("name")

    def location(self, obj):
        return obj.get_absolute_url()

    def lastmod(self, obj):
        return obj.last_loaded


class YearSitemap(Sitemap):
    """Sitemap for /during/<year>/ pages, keyed by data-coverage years."""

    priority = 0.5
    changefreq = "weekly"

    def items(self):
        from works.views_indexed import data_year_counts

        return sorted(data_year_counts().keys(), reverse=True)

    def location(self, year):
        return reverse("optimap:during-year", kwargs={"year": year})


class TopicSitemap(Sitemap):
    """Sitemap for /on/<topic>/ pages (OpenAlex topics on published works)."""

    priority = 0.5
    changefreq = "weekly"

    def items(self):
        from works.views_indexed import topic_slug_map

        return sorted(topic_slug_map().keys())

    def location(self, topic_slug):
        return reverse("optimap:on-topic", kwargs={"topic_slug": topic_slug})


def _published_sources():
    return (
        Source.objects.exclude(slug__isnull=True)
        .annotate(n=Count("works", filter=Q(works__status="p")))
        .filter(n__gt=0)
        .order_by("name")
    )


class SourceIndexSitemap(Sitemap):
    """Sitemap for /in/<source>/ landing pages with published works."""

    priority = 0.6
    changefreq = "weekly"

    def items(self):
        return list(_published_sources())

    def location(self, obj):
        return obj.get_absolute_url()


class SourceFeedsSitemap(Sitemap):
    """Sitemap for per-source GeoRSS and Atom feed URLs (#253)."""

    priority = 0.6
    changefreq = "daily"

    def items(self):
        return [(s, fmt) for s in _published_sources() for fmt in ("rss", "atom")]

    def location(self, item):
        source, fmt = item
        name = "api-source-georss" if fmt == "rss" else "api-source-atom"
        return reverse(f"optimap:{name}", kwargs={"source_slug": source.slug})


class AuthorSitemap(Sitemap):
    """Sitemap for /by/<orcid>/ author facet pages (issue #29)."""

    priority = 0.5
    changefreq = "weekly"

    def items(self):
        return (
            Author.objects.filter(orcid__isnull=False)
            .annotate(_work_count=Count("works", filter=Q(works__status="p")))
            .filter(_work_count__gt=0)
            .order_by("name")
        )

    def location(self, obj):
        return reverse("optimap:by-author", kwargs={"orcid": obj.orcid})

    def lastmod(self, obj):
        return obj.last_seen
