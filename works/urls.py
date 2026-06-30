# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""OPTIMAP urls."""

from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView

from optimap import views as general_views
from works import views as work_views
from works import (
    views_collections,
    views_gazetteer,
    views_geometry,
    views_indexed,
    views_regions,
    views_sources,
)
from works.api import router as publications_router
from works.bok import views as bok_views

from .feeds import CollectionGeoFeed, GlobalGeoFeed, RegionalGeoFeed, SourceGeoFeed

app_name = "optimap"

urlpatterns = [
    # General pages
    path("", general_views.main, name="main"),
    path("about/", general_views.about, name="about"),
    path("accessibility/", general_views.accessibility, name="accessibility"),
    path("privacy/", general_views.privacy, name="privacy"),
    path("data/", general_views.data, name="data"),
    path(
        "data/regenerate/",
        general_views.schedule_data_dump_regeneration,
        name="schedule-data-dump",
    ),
    path("pages/", general_views.sitemap_page, name="sitemap-page"),
    path("regions/", general_views.feeds, name="feeds"),
    path("feeds/", RedirectView.as_view(pattern_name="optimap:feeds", permanent=True)),
    path("geoextent/", general_views.geoextent, name="geoextent"),
    path("statistics/", work_views.statistics_page, name="statistics"),
    # Admin
    path("admin/", admin.site.urls),
    # API
    path("api/", lambda request: redirect("v1/", permanent=True), name="api"),
    path("api/v1/", include((publications_router.urls, "works"), namespace="works"), name="api_current"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/schema/ui/", SpectacularRedocView.as_view(url_name="optimap:schema"), name="redoc"),
    # API v1 Gazetteer proxy endpoints
    path("api/v1/gazetteer/<str:provider>/search/", views_gazetteer.gazetteer_search, name="gazetteer-search"),
    path("api/v1/gazetteer/<str:provider>/reverse/", views_gazetteer.gazetteer_reverse, name="gazetteer-reverse"),
    # API v1 Body of Knowledge (EO4GEO BoK) autosuggest
    path("api/v1/bok/search/", bok_views.bok_search, name="bok-search"),
    # API v1 Feed endpoints - GeoRSS format (with .rss extension)
    path("api/v1/feeds/optimap-global.rss", GlobalGeoFeed(feed_type_variant="georss"), name="api-feed-georss"),
    path(
        "api/v1/feeds/optimap-<slug:continent_slug>.rss",
        RegionalGeoFeed(feed_type_variant="georss"),
        name="api-continent-georss",
    ),
    path(
        "api/v1/feeds/optimap-<slug:ocean_slug>.rss",
        RegionalGeoFeed(feed_type_variant="georss"),
        name="api-ocean-georss",
    ),
    # API v1 Feed endpoints - Atom format (with .atom extension)
    path("api/v1/feeds/optimap-global.atom", GlobalGeoFeed(feed_type_variant="atom"), name="api-feed-atom"),
    path(
        "api/v1/feeds/optimap-<slug:continent_slug>.atom",
        RegionalGeoFeed(feed_type_variant="atom"),
        name="api-continent-atom",
    ),
    path(
        "api/v1/feeds/optimap-<slug:ocean_slug>.atom", RegionalGeoFeed(feed_type_variant="atom"), name="api-ocean-atom"
    ),
    # API v1 Feed endpoints - Collection feeds
    path(
        "api/v1/feeds/collection-<slug:collection_slug>.rss",
        CollectionGeoFeed(feed_type_variant="georss"),
        name="api-collection-georss",
    ),
    path(
        "api/v1/feeds/collection-<slug:collection_slug>.atom",
        CollectionGeoFeed(feed_type_variant="atom"),
        name="api-collection-atom",
    ),
    # API v1 Feed endpoints - Source feeds (#253)
    path(
        "api/v1/feeds/source-<slug:source_slug>.rss",
        SourceGeoFeed(feed_type_variant="georss"),
        name="api-source-georss",
    ),
    path(
        "api/v1/feeds/source-<slug:source_slug>.atom",
        SourceGeoFeed(feed_type_variant="atom"),
        name="api-source-atom",
    ),
    # Region HTML pages (human-readable)
    path("regions/continent/<slug:continent_slug>/", views_regions.continent_feed_page, name="feed-continent-page"),
    path("regions/ocean/<slug:ocean_slug>/", views_regions.ocean_feed_page, name="feed-ocean-page"),
    # Staff region curation (works with geometry but no region)
    path("regions/curate/work/<int:work_id>/", views_regions.set_work_region, name="set-work-region"),
    path("regions/curate/backfill/", views_regions.trigger_region_backfill, name="trigger-region-backfill"),
    path(
        "feeds/continent/<slug:continent_slug>/",
        RedirectView.as_view(pattern_name="optimap:feed-continent-page", permanent=True),
    ),
    path(
        "feeds/ocean/<slug:ocean_slug>/", RedirectView.as_view(pattern_name="optimap:feed-ocean-page", permanent=True)
    ),
    # Collections
    path("collections/", views_collections.collections_index, name="collections"),
    # ID-based URL must precede the slug pattern below — Django's <slug:>
    # converter matches digits too, so without this ordering numeric URLs
    # would dispatch into the slug view and 404 on the lookup.
    path("collections/<int:collection_id>/publish/", views_collections.publish_collection, name="publish-collection"),
    path(
        "collections/<int:collection_id>/unpublish/",
        views_collections.unpublish_collection,
        name="unpublish-collection",
    ),
    path(
        "collections/<int:collection_id>/publish-works/",
        views_collections.publish_collection_works,
        name="publish-collection-works",
    ),
    path(
        "collections/<int:collection_id>/description/",
        views_collections.update_collection_description,
        name="update-collection-description",
    ),
    path(
        "collections/<int:collection_id>/logo/",
        views_collections.update_collection_logo,
        name="update-collection-logo",
    ),
    path(
        "collections/<int:collection_id>/curators/add/", views_collections.add_curator, name="collection-add-curator"
    ),
    path(
        "collections/<int:collection_id>/curators/<int:user_id>/remove/",
        views_collections.remove_curator,
        name="collection-remove-curator",
    ),
    path("collections/<int:collection_id>/", views_collections.collection_by_id_redirect, name="collection-by-id"),
    path(
        "collections/<slug:collection_slug>/geojson/", views_collections.collection_geojson, name="collection-geojson"
    ),
    path("collections/<slug:collection_slug>/", views_collections.collection_page, name="collection-page"),
    path(
        "work/<int:work_id>/collection/<int:collection_id>/add/",
        views_collections.add_work_to_collection,
        name="add-work-to-collection",
    ),
    path(
        "work/<int:work_id>/collection/<int:collection_id>/remove/",
        views_collections.remove_work_from_collection,
        name="remove-work-from-collection",
    ),
    # Data downloads (global — all published works)
    path("download/geojson/", work_views.download_geojson, name="download_geojson"),
    path("download/geopackage/", work_views.download_geopackage, name="download_geopackage"),
    path("download/csv/", work_views.download_csv, name="download_csv"),
    # Data downloads (per-collection — #217)
    path(
        "api/v1/collections/<slug:collection_slug>/download/geojson/",
        work_views.download_collection_geojson,
        name="download-collection-geojson",
    ),
    path(
        "api/v1/collections/<slug:collection_slug>/download/gpkg/",
        work_views.download_collection_gpkg,
        name="download-collection-gpkg",
    ),
    path(
        "api/v1/collections/<slug:collection_slug>/download/csv/",
        work_views.download_collection_csv,
        name="download-collection-csv",
    ),
    # Works
    path("works/", work_views.works_list, name="works"),
    path("contribute/next/", work_views.contribute_next, name="contribute-next"),
    path("contribute/", work_views.contribute, name="contribute"),
    # Unified work URLs - accepts DOI, ID, or other identifiers
    # Note: path:identifier accepts any string including slashes (for DOIs) and numbers (for IDs)
    path(
        "work/<path:identifier>/contribute-geometry/", views_geometry.contribute_geometry, name="contribute-geometry"
    ),
    path("work/<path:identifier>/contribute-bok/", views_geometry.contribute_bok, name="contribute-bok"),
    path("work/<path:identifier>/publish/", views_geometry.publish_work, name="publish-work"),
    path("work/<path:identifier>/unpublish/", views_geometry.unpublish_work, name="unpublish-work"),
    path("work/<path:identifier>/reharvest/", views_geometry.reharvest_work, name="reharvest-work"),
    path("work/<path:identifier>/preview.png", work_views.work_preview_png, name="work-preview"),
    path("work/<path:identifier>/", work_views.work_landing, name="work-landing"),
    # Authentication/User management
    path("login/<str:token>", work_views.authenticate_via_magic_link, name="magic_link"),
    path("loginconfirm/", work_views.confirmation_login, name="loginconfirm"),
    path("loginres/", work_views.loginres, name="login_response"),
    path("logout/", work_views.customlogout, name="logout"),
    path("usersettings/", work_views.user_settings, name="usersettings"),
    path("usersettings/random-username/", work_views.random_recognition_username, name="random_recognition_username"),
    path("recognition-board/", work_views.recognition_board, name="recognition_board"),
    path("subscriptions/", work_views.user_subscriptions, name="subscriptions"),
    path("addsubscriptions/", work_views.add_subscriptions, name="addsubscriptions"),
    path("unsubscribe/", work_views.unsubscribe, name="unsubscribe"),
    path("changeuser/", work_views.change_useremail, name="changeuser"),
    path("confirm-email/<str:token>/<str:email_new>/", work_views.confirm_email_change, name="confirm_email_change"),
    path("request-delete/", work_views.request_delete, name="request_delete"),
    path("confirm-delete/<str:token>/", work_views.confirm_account_deletion, name="confirm_delete"),
    path("finalize-delete/", work_views.finalize_account_deletion, name="finalize_delete"),
    # Redirects
    path("favicon.ico", lambda request: redirect("static/favicon.ico", permanent=True)),
    path("contact/", RedirectView.as_view(pattern_name="optimap:about", permanent=True), name="contact"),
    path("imprint/", RedirectView.as_view(pattern_name="optimap:about", permanent=True)),
    # Legacy feed URLs - redirect to new API v1 endpoints
    path("feed/", RedirectView.as_view(pattern_name="optimap:api-feed-georss", permanent=True)),
    path(
        "feed/geoatom/",
        RedirectView.as_view(pattern_name="optimap:api-feed-atom", permanent=True),
        name="geoatom_feed",
    ),
    path(
        "feed/georss/",
        RedirectView.as_view(pattern_name="optimap:api-feed-georss", permanent=True),
        name="georss_feed",
    ),
    path(
        "feed/w3cgeo/",
        RedirectView.as_view(pattern_name="optimap:api-feed-georss", permanent=True),
        name="w3cgeo_feed",
    ),
    # Faceted permalink pages (#29) + source landing pages (#253).
    # `in/<slug>/` is the unified source landing page (work list + coverage + feeds).
    # Index pages (no slug) must come before the <slug> patterns.
    path("browse/", views_indexed.browse_page, name="browse"),
    path("countries/", views_indexed.countries_overview, name="countries"),
    path("countries/curate/work/<int:work_id>/", views_indexed.set_work_country, name="set-work-country"),
    path("countries/curate/backfill/", views_indexed.trigger_country_backfill, name="trigger-country-backfill"),
    path("at/", views_indexed.place_index, name="at-index"),
    path("at/<slug:place_slug>/", views_indexed.place_page, name="at-place"),
    path("during/<int:year>/", views_indexed.year_page, name="during-year"),
    path("on/<slug:topic_slug>/", views_indexed.topic_page, name="on-topic"),
    path("by/", views_indexed.author_index, name="by-index"),
    path("by/<str:orcid>/", views_indexed.author_page, name="by-author"),
    path("in/", views_sources.source_index, name="in-index"),
    path("in/<slug:source_slug>/", views_sources.source_page, name="in-source"),
    # Collection vanity short URL — must be last so the explicit patterns above win.
    # Resolves only when a Collection has the matching short_slug; otherwise 404.
    path("<slug:short_slug>/", views_collections.collection_short_redirect, name="collection-short-redirect"),
]
