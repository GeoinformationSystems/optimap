"""OPTIMAP urls."""

from django.urls import path, include
from django.shortcuts import redirect
from publications import views
from .feeds import GeoFeed
from django.views.generic import RedirectView
from .feeds_geometry import GeoFeedByGeometry

from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView

app_name = "optimap"

urlpatterns = [
    path('', views.main, name="main"),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/schema/ui/', SpectacularRedocView.as_view(url_name='optimap:schema'), name='redoc'),
    path('download/geojson/', views.download_geojson, name='download_geojson'),
    path('download/geopackage/', views.download_geopackage, name='download_geopackage'),
    path('favicon.ico', lambda request: redirect('static/favicon.ico', permanent=True)),
    path('feed/', RedirectView.as_view(pattern_name='optimap:georss_feed', permanent=True)),
    path('feed/geoatom/', GeoFeed(feed_type_variant="geoatom"), name='geoatom_feed'),
    path('feed/georss/', GeoFeed(feed_type_variant="georss"), name='georss_feed'),
    path('feed/w3cgeo/', GeoFeed(feed_type_variant="w3cgeo"), name='w3cgeo_feed'),
    path("about/", views.about, name="about"),
    path("accessibility/", views.accessibility, name="accessibility"),
    path("addsubscriptions/", views.add_subscriptions, name="addsubscriptions"),
    path("api", lambda request: redirect('/api/v1/', permanent=False), name="api"),
    path("api/", lambda request: redirect('/api/v1/', permanent=False)),
    path("api/v1", lambda request: redirect('/api/v1/', permanent=False)),
    path("api/v1/", include("publications.api")),
    path("changeuser/", views.change_useremail, name="changeuser"),
    path("confirm-delete/<str:token>/", views.confirm_account_deletion, name="confirm_delete"),
    path("confirm-email/<str:token>/<str:email_new>/", views.confirm_email_change, name="confirm-email-change"),
    path("contact/", RedirectView.as_view(pattern_name='about', permanent=True)),
    path("data/", views.data, name="data"),
    path("finalize-delete/", views.finalize_account_deletion, name="finalize_delete"),
    path("imprint/", RedirectView.as_view(pattern_name='about', permanent=True)),
    path("login/<str:token>", views.authenticate_via_magic_link, name="magic_link"),
    path("loginconfirm/", views.confirmation_login, name="loginconfirm"),
    path("loginres/", views.loginres, name="loginres"),
    path("logout/", views.customlogout, name="logout"),
    path("privacy/", views.privacy, name="privacy"),
    path("request-delete/", views.request_delete, name="request_delete"),
    path("subscriptions/", views.user_subscriptions, name="subscriptions"),
    path("unsubscribe/", views.unsubscribe, name="unsubscribe"),
    path("usersettings/", views.user_settings, name="usersettings"),
    path("feeds/georss/<slug:geometry_slug>/",
         GeoFeedByGeometry(feed_type_variant="georss"), name="feed-georss-by-slug",),
    path("feeds/geoatom/<slug:geometry_slug>/",
         GeoFeedByGeometry(feed_type_variant="geoatom"), name="feed-geoatom-by-slug"),
    path("feeds/", views.feeds_list, name="feeds_list"),

]
