# SPDX-FileCopyrightText: 2023 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

from django.contrib.sitemaps import Sitemap
from django.db.models import Count, Q
from django.urls import reverse

from .models import Author, GlobalRegion, Work


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
