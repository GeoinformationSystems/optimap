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
from django.contrib import admin
from django.urls import path, re_path, include
from django.contrib.sitemaps import views as sitemaps_views
from publications.sitemaps import PublicationsSitemap, StaticViewSitemap
from publications.views import RobotsView

sitemaps = {
    "static": StaticViewSitemap,
    "publications": PublicationsSitemap,
}

urlpatterns = [
    path('admin/', admin.site.urls),
    # Updated inclusion with namespace
    path('', include(('publications.urls', 'publications'), namespace='publications')),
    path(
        "sitemap.xml",
        sitemaps_views.index,
        {"sitemaps": sitemaps},
        name="django.contrib.sitemaps.views.index",
    ),
    path(
        "sitemap-<section>.xml",
        sitemaps_views.sitemap,
        {"sitemaps": sitemaps},
        name="django.contrib.sitemaps.views.sitemap",
    ),
    re_path(r'^robots.txt', RobotsView.as_view(), name="robots_file"),
]

# Context processor for the site
from django.contrib.sites.shortcuts import get_current_site
from django.utils.functional import SimpleLazyObject

def site(request):
    protocol = 'https' if request.is_secure() else 'http'
    site = SimpleLazyObject(lambda: "{0}://{1}".format(protocol, get_current_site(request)))
    return {'site': site}
