"""OPTIMAP urls."""

from django.urls import path, include
from django.shortcuts import redirect
from publications import views
from publications.api import JournalViewSet  # Import the JournalViewSet
from .feeds import OptimapFeed, atomFeed
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView
from rest_framework.routers import DefaultRouter

app_name = "optimap"

# Define a router for DRF viewsets
router = DefaultRouter()
router.register(r'journals', JournalViewSet, basename='journal')  # Register the JournalViewSet

urlpatterns = [
    path('', views.main, name="main"),
    path('favicon.ico', lambda request: redirect('static/favicon.ico', permanent=True)),
    path("api", lambda request: redirect('/api/v1/', permanent=False), name="api"),
    path("api/", lambda request: redirect('/api/v1/', permanent=False)),
    path("api/v1", lambda request: redirect('/api/v1/', permanent=False)),
    path("api/v1/", include("publications.api")),  # Existing API endpoints
    path("api/v1/", include(router.urls)),  # Include the router for journals API
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
    path("delete/", views.delete_account, name="delete"),
    path("changeuser/", views.change_useremail, name="changeuser"),
]
