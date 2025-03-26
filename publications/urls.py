"""OPTIMAP urls."""

from django.urls import path, include
from django.shortcuts import redirect
from publications import views
from .feeds import OptimapFeed
from .feeds import atomFeed
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView

app_name = "optimap"

urlpatterns = [
    path('', views.main, name="main"),
    path('favicon.ico', lambda request: redirect('static/favicon.ico', permanent=True)),
    path("api", lambda request: redirect('/api/v1/', permanent=False), name="api"),
    path("api/", lambda request: redirect('/api/v1/', permanent=False)),
    path("api/v1", lambda request: redirect('/api/v1/', permanent=False)),
    path("api/v1/", include("publications.api")),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/schema/ui/sitemap', SpectacularRedocView.as_view(url_name='optimap:schema'), name='redoc'),
    path("data/", views.data, name="data"),
    path('feed/rss', OptimapFeed(), name="GeoRSSfeed"), 
    path("feed/atom", atomFeed(), name="GeoAtomfeed"),
    path("loginres/", views.loginres, name="loginres"),
    path("privacy/", views.privacy, name="privacy"),
    path("loginconfirm/", views.Confirmationlogin, name="loginconfirm"),
    path("login/<str:token>", views.authenticate_via_magic_link, name="magic_link"),
    path("logout/", views.customlogout, name="logout"),
    path("usersettings/", views.user_settings, name="usersettings"),
    path("subscriptions/", views.user_subscriptions, name="subscriptions"),
    path("addsubscriptions/", views.add_subscriptions, name="addsubscriptions"),
    path("request-delete/", views.request_delete, name="request_delete"),
    path("confirm-delete/<str:token>/", views.confirm_account_deletion, name="confirm_delete"),
    path("finalize-delete/", views.finalize_account_deletion, name="finalize_delete"),
    path("changeuser/", views.change_useremail, name="changeuser"),
    path('download/geojson/', views.download_geojson, name='download_geojson'),
    
]
