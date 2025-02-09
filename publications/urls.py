"""OPTIMAP urls."""

from django.urls import path, include
from django.shortcuts import redirect
from publications import views
from .feeds import OptimapFeed, atomFeed
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView

app_name = "optimap"

urlpatterns = [
    path('', views.main, name="main"),
    path('favicon.ico', lambda request: redirect('static/favicon.ico', permanent=True)),

    # API Endpoints
    path("api", lambda request: redirect('/api/v1/', permanent=False), name="api"),
    path("api/", lambda request: redirect('/api/v1/', permanent=False)),
    path("api/v1", lambda request: redirect('/api/v1/', permanent=False)),
    path("api/v1/", include("publications.api")),

    # API Documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/schema/ui/sitemap', SpectacularRedocView.as_view(url_name='optimap:schema'), name='redoc'),

    # Data & Feeds
    path("data/", views.data, name="data"),
    path('feed/rss', OptimapFeed(), name="GeoRSSfeed"), 
    path("feed/atom", atomFeed(), name="GeoAtomfeed"),

    # Authentication
    path("loginres/", views.loginres, name="loginres"),
    path("privacy/", views.privacy, name="privacy"),
    path("loginconfirm/", views.Confirmationlogin, name="loginconfirm"),
    path("login/<str:token>", views.authenticate_via_magic_link, name="magic_link"),
    path("logout/", views.customlogout, name="logout"),

    # User Settings & Account Management
    path("usersettings/", views.user_settings, name="usersettings"),
    path("delete/", views.delete_account, name="delete"),
    path("changeuser/", views.change_useremail, name="changeuser"),

    # Subscription Management (FIXED function name)
    path("subscriptions/", views.list_subscriptions, name="subscriptions"),
    path("subscriptions/add/", views.add_subscription, name="add_subscription"),  # ✅ Fixed function name
    path("subscriptions/edit/<int:subscription_id>/", views.edit_subscription, name="edit_subscription"),
    path("subscriptions/delete/<int:subscription_id>/", views.delete_subscription, name="delete_subscription"),
]
