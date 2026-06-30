# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""OPTIMAP URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.contrib import admin
from django.contrib.sitemaps import views as sitemaps_views
from django.urls import include, path, re_path
from django.views.decorators.cache import cache_page

from optimap.sitemaps import (
    AuthorSitemap,
    CollectionDownloadsSitemap,
    CollectionFeedsSitemap,
    CollectionsSitemap,
    CountrySitemap,
    FeedsSitemap,
    SourceFeedsSitemap,
    SourceIndexSitemap,
    StaticViewSitemap,
    TopicSitemap,
    WorksSitemap,
    YearSitemap,
)
from optimap.views import RobotsView, sitemap_index_gz, sitemap_section_gz

sitemaps = {
    "static": StaticViewSitemap,
    "works": WorksSitemap,
    "feeds": FeedsSitemap,
    "collections": CollectionsSitemap,
    "collection-feeds": CollectionFeedsSitemap,
    "collection-downloads": CollectionDownloadsSitemap,
    "countries": CountrySitemap,
    "years": YearSitemap,
    "topics": TopicSitemap,
    "authors": AuthorSitemap,
    "sources-index": SourceIndexSitemap,
    "source-feeds": SourceFeedsSitemap,
}

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "sitemap.xml",
        cache_page(settings.PAGE_CACHE_LONG, cache="memory")(sitemaps_views.index),
        {"sitemaps": sitemaps},
        name="django.contrib.sitemaps.views.index",
    ),
    path(
        "sitemap-<section>.xml",
        cache_page(settings.PAGE_CACHE_LONG, cache="memory")(sitemaps_views.sitemap),
        {"sitemaps": sitemaps},
        name="django.contrib.sitemaps.views.sitemap",
    ),
    path(
        "sitemap.xml.gz",
        sitemap_index_gz,
        {"sitemaps": sitemaps},
        name="sitemap-index-gz",
    ),
    path(
        "sitemap-<section>.xml.gz",
        sitemap_section_gz,
        {"sitemaps": sitemaps},
        name="sitemap-section-gz",
    ),
    re_path(r"^robots.txt", RobotsView.as_view(), name="robots_file"),
]

# OGC API - Features via pygeoapi at /ogcapi/ — registered before works.urls to
# prevent the <slug:short_slug> catch-all in works/urls.py from intercepting it.
if getattr(settings, "PYGEOAPI_ENABLED", False):
    from pathlib import Path as _Path

    import pygeoapi as _pygeoapi_pkg
    from django.views.static import serve as _static_serve

    _pygeoapi_static = str(_Path(_pygeoapi_pkg.__file__).parent / "static")
    urlpatterns += [
        re_path(r"^ogcapi/static/(?P<path>.*)$", _static_serve, {"document_root": _pygeoapi_static}),
    ]
    urlpatterns += [path("ogcapi/", include("pygeoapi.django_.urls"))]

# Main app URLs — must come AFTER the ogcapi prefix so the slug catch-all
# in works/urls.py doesn't shadow /ogcapi/.
urlpatterns += [
    path("", include(("works.urls", "optimap"), namespace="optimap")),
]

# Custom error handlers
handler404 = "optimap.views.custom_404"
handler500 = "optimap.views.custom_500"

# Context processor for the site
from django.contrib.sites.shortcuts import get_current_site
from django.utils.functional import SimpleLazyObject


def site(request):
    protocol = "https" if request.is_secure() else "http"
    site = SimpleLazyObject(lambda: "{0}://{1}".format(protocol, get_current_site(request)))
    return {"site": site}
