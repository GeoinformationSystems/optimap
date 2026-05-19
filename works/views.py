# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
logger = logging.getLogger(__name__)

from django.contrib.auth import login, logout
from django.shortcuts import render, redirect, get_object_or_404
from django.core.cache import cache
from django.http import HttpResponseRedirect, HttpResponse, FileResponse, Http404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET
from django.core.mail import EmailMessage, send_mail, get_connection
from django.views.generic import View
import secrets
from django.contrib import messages
from django.views.decorators.cache import never_cache
from django.urls import reverse
import uuid
from django.utils.timezone import get_default_timezone
from datetime import datetime
import imaplib
import time
from math import floor
from urllib.parse import unquote
from django.conf import settings
from django.contrib.gis.geos import GeometryCollection
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q
from works.models import BlockedEmail, BlockedDomain, Subscription, UserProfile, Work, GlobalRegion, Collection
from works.feeds import normalize_region_slug
from works.utils.statistics import get_cached_statistics
from django.contrib.auth import get_user_model
User = get_user_model()
import tempfile, os
from pathlib import Path
from works.tasks import regenerate_geojson_cache, regenerate_geopackage_cache
from osgeo import ogr, osr
ogr.UseExceptions()
import humanize
import json
import re


LOGIN_TOKEN_LENGTH  = 32
LOGIN_TOKEN_TIMEOUT_SECONDS = 10 * 60
EMAIL_CONFIRMATION_TIMEOUT_SECONDS = 10 * 60
ACCOUNT_DELETE_TOKEN_TIMEOUT_SECONDS = 10 * 60
USER_DELETE_TOKEN_PREFIX = "user_delete_token"
EMAIL_CONFIRMATION_TOKEN_PREFIX = "email_confirmation_"

# ------------------------------
# Download Endpoints
# ------------------------------

@require_GET
def download_geojson(request):
    """
    Returns the latest GeoJSON dump file, gzipped if the client accepts it,
    but always with Content-Type: application/json.
    """
    cache_dir = Path(tempfile.gettempdir()) / "optimap_cache"
    cache_dir.mkdir(exist_ok=True)
    json_path = regenerate_geojson_cache()
    gzip_path = Path(str(json_path) + ".gz")
    accept_enc = request.META.get('HTTP_ACCEPT_ENCODING', '')

    if 'gzip' in accept_enc and gzip_path.exists():
        response = FileResponse(
            open(gzip_path, 'rb'),
            content_type="application/json",
            as_attachment=True,
            filename=gzip_path.name
        )
        response['Content-Encoding'] = 'gzip'
        response['Content-Disposition'] = f'attachment; filename="{gzip_path.name}"'
    else:
        # Serve the plain JSON
        response = FileResponse(
            open(json_path, 'rb'),
            content_type="application/json",
            as_attachment=True,
            filename=Path(json_path).name
        )
        response['Content-Disposition'] = f'attachment; filename="{Path(json_path).name}"'
    return response


def generate_geopackage():
    cache_dir = os.path.join(tempfile.gettempdir(), "optimap_cache")
    os.makedirs(cache_dir, exist_ok=True)
    gpkg_path = os.path.join(cache_dir, "publications.gpkg")

    driver = ogr.GetDriverByName("GPKG")
    if os.path.exists(gpkg_path):
        driver.DeleteDataSource(gpkg_path)
    ds = driver.CreateDataSource(gpkg_path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    layer = ds.CreateLayer("works", srs, ogr.wkbGeometryCollection)

    for name in ("title", "abstract", "doi", "source"):
        field_defn = ogr.FieldDefn(name, ogr.OFTString)
        field_defn.SetWidth(255)
        layer.CreateField(field_defn)

    layer_defn = layer.GetLayerDefn()
    for work in Work.objects.all():
        feat = ogr.Feature(layer_defn)
        feat.SetField("title", work.title or "")
        feat.SetField("abstract", work.abstract or "")
        feat.SetField("doi", work.doi or "")
        feat.SetField("source", work.source.name if work.source else "")
        if work.geometry:
            wkb = work.geometry.wkb
            geom = ogr.CreateGeometryFromWkb(wkb)
            geom.AssignSpatialReference(srs)
            feat.SetGeometry(geom)
        layer.CreateFeature(feat)
        feat = None

    ds = None
    return gpkg_path

@require_GET
def download_geopackage(request):
    """
    Returns the latest GeoPackage dump file.
    """
    gpkg_path = regenerate_geopackage_cache()
    if not gpkg_path or not os.path.exists(gpkg_path):
        raise Http404("GeoPackage not available.")
    return FileResponse(open(gpkg_path, 'rb'), as_attachment=True, filename=os.path.basename(gpkg_path))


def main(request):
    # Pass the 'next' parameter to template for login redirect
    next_url = request.GET.get('next', '')
    return render(request, "main.html", {'next': next_url})

def contribute(request):
    """
    Page showing harvested works that need spatial or temporal extent contributions.
    Displays works with harvested status that are missing geometry or temporal extent.
    """
    # Get publications that are harvested and missing spatial OR temporal extent
    publications_query = Work.objects.filter(
        status='h',  # Harvested status
    ).filter(
        Q(geometry__isnull=True) |  # NULL geometry
        Q(geometry__isempty=True) |  # Empty GeometryCollection
        Q(timeperiod_startdate__isnull=True) |  # NULL start date
        Q(timeperiod_enddate__isnull=True)      # NULL end date
    ).order_by('-creationDate')

    total_count = publications_query.count()
    publications_needing_contribution = publications_query

    context = {
        'works': publications_needing_contribution,
        'total_count': total_count,
    }
    return render(request, 'contribute.html', context)

def about(request):
    return render(request, 'about.html')

def accessibility(request):
    return render(request, 'accessibility.html')


def feeds_list(request):
    """Display available predefined feeds grouped by global regions."""
    regions = GlobalRegion.objects.all().order_by("name")
    context = {
        "regions": regions,
        "ocean_simplification_tolerance": settings.OCEAN_SIMPLIFICATION_TOLERANCE,
        "ocean_simplification_percentile": settings.OCEAN_SIMPLIFICATION_PERCENTILE,
    }
    return render(request, "feeds.html", context)

def loginres(request):
    email = request.POST.get('email', False)
    if is_email_blocked(email):
        logger.warning('Attempted login with blocked email: %s', email)
        return render(request, "error.html", {
            'error': {
                'class': 'danger',
                'title': 'Login failed!',
                'text': f"You attempted to login using an email that is blocked. Please contact support for assistance: <a href=\"{request.site.domain}/contact\">{request.site.domain}/contact</a>"
                }
        })
    else:
        subject = 'OPTIMAP Login'
        link = get_login_link(request, email)
        valid = floor(LOGIN_TOKEN_TIMEOUT_SECONDS / 60)
        body = f"""Hello {email} !

You requested that we send you a link to log in to OPTIMAP at {request.site.domain}:

{link}

Please click on the link to log in.
The link is valid for {valid} minutes.
"""
        logger.info('Login process started for user %s', email)
        try:
            email_message = EmailMessage(
                subject=subject,
                body=body,
                from_email=settings.EMAIL_HOST_USER,
                to=[email],
                headers={'OPTIMAP': request.site.domain}
            )
            result = email_message.send()
            logger.info('%s sent login email to %s with the result: %s', settings.EMAIL_HOST_USER, email_message.recipients(), result)
        except Exception as ex:
            logger.exception('Error sending login email to %s from %s', email, settings.EMAIL_HOST_USER)
            logger.error(ex)
            return render(request, "error.html", {
                'error': {
                    'class': 'danger',
                    'title': 'Login failed!',
                    'text': 'Error sending the login email. Please try again or contact us!'
                }
            })

        try:
            if str(get_connection().__class__.__module__).endswith("smtp"):
                with imaplib.IMAP4_SSL(settings.EMAIL_HOST_IMAP, port=settings.EMAIL_PORT_IMAP) as imap:
                    message = str(email_message.message()).encode()
                    imap.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
                    folder = settings.EMAIL_IMAP_SENT_FOLDER
                    imap.append('"{folder}"', '\\Seen', imaplib.Time2Internaldate(time.time()), str(message).encode('utf-8'))
                    logger.debug('Saved email to IMAP folder "%s"', folder)
        except Exception as ex:
            logger.exception('Error saving sent email to %s for %s', email, settings.EMAIL_HOST_USER)
            logger.error(ex)

        return render(request, 'login_response.html', {'email': email, 'valid_minutes': valid})

def privacy(request):
    return render(request, 'privacy.html')

@never_cache
def data(request):
    """
    Renders the data page showing links and sizes for the latest dumps.
    """
    cache_dir = Path(tempfile.gettempdir()) / "optimap_cache"
    cache_dir.mkdir(exist_ok=True)

    # scan for existing dumps
    geojson_files = sorted(cache_dir.glob('optimap_data_dump_*.geojson'), reverse=True)
    gpkg_files    = sorted(cache_dir.glob('optimap_data_dump_*.gpkg'),   reverse=True)

    last_geo  = geojson_files[0] if geojson_files else None
    last_gzip = Path(str(last_geo) + ".gz") if last_geo else None
    last_gpkg = gpkg_files[0]    if gpkg_files    else None

    # — Supervisor check: ensure all dump file times are within 1 hour
    mtimes = []
    for p in (last_geo, last_gzip, last_gpkg):
        if p and p.exists():
            mtimes.append(p.stat().st_mtime)
    if mtimes and (max(mtimes) - min(mtimes) > 3600):
        ts_map = {
            p.name: datetime.fromtimestamp(p.stat().st_mtime, get_default_timezone())
            for p in (last_geo, last_gzip, last_gpkg) if p and p.exists()
        }
        logger.warning("Data dump timestamps differ by >1h: %s", ts_map)

    # humanized sizes
    geojson_size    = humanize.naturalsize(last_geo.stat().st_size, binary=True) if last_geo else None
    geopackage_size = humanize.naturalsize(last_gpkg.stat().st_size, binary=True) if last_gpkg else None

    # last updated timestamp (using JSON file)
    if last_geo:
        ts = last_geo.stat().st_mtime
        last_updated = datetime.fromtimestamp(ts, get_default_timezone())
    else:
        last_updated = None

    # Get latest Zenodo deposition info
    # In DEBUG mode, show sandbox depositions; in production, show only production depositions
    from works.models import ZenodoDepositionLog

    if settings.DEBUG:
        # Debug mode: show sandbox depositions
        latest_zenodo = ZenodoDepositionLog.objects.filter(
            status='success',
            api_base__icontains='sandbox.zenodo.org'
        ).order_by('-deposition_date').first()
    else:
        # Production mode: show only production depositions (exclude sandbox)
        latest_zenodo = ZenodoDepositionLog.objects.filter(
            status='success'
        ).exclude(
            api_base__icontains='sandbox.zenodo.org'
        ).order_by('-deposition_date').first()

    return render(request, 'data.html', {
        'geojson_size':    geojson_size,
        'geopackage_size': geopackage_size,
        'interval':        settings.DATA_DUMP_INTERVAL_HOURS,
        'last_updated':    last_updated,
        'last_geojson':    last_geo.name  if last_geo else None,
        'last_gpkg':       last_gpkg.name if last_gpkg else None,
        'latest_zenodo':   latest_zenodo,
    })

def confirmation_login(request):
    return render(request, 'confirmation_login.html')


def login_user(request, user):
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    user.save()

@require_GET
def authenticate_via_magic_link(request, token):
    cache_data = cache.get(token)
    logger.info('Authenticating magic link with token %s: Found data: %s', token, cache_data)

    if cache_data is None:
        logger.debug('Magic link invalid or expired')
        return render(request, "error.html", {
            'error': {
                'class': 'danger',
                'title': 'Authentication failed!',
                'text': 'Magic link invalid or expired. Please try again!'
            }
        })

    # Extract email and next URL from cache data
    email = cache_data.get('email')
    next_url = cache_data.get('next', '/')

    user = User.objects.filter(email=email).first()
    if user:
        is_new = False
        needs_confirmation = False
        login_user(request, user)
        # Store next URL in session for redirect after confirmation
        request.session['login_redirect_url'] = next_url
    elif request.GET.get('confirmed', None) == 'true':
        user = User.objects.create_user(username=email, email=email)
        is_new = True
        needs_confirmation = False
        login_user(request, user)
        # Redirect to next URL after successful login
        logger.info('User %s logged in successfully, redirecting to %s', email, next_url)
        return redirect(next_url)
    else:
        is_new = True
        needs_confirmation = True
        # Store next URL for redirect after confirmation
        request.session['login_redirect_url'] = next_url

    # If user is already authenticated and doesn't need confirmation, redirect
    if not needs_confirmation and user:
        logger.info('User %s authenticated, redirecting to %s', email, next_url)
        return redirect(next_url)

    return render(request, "confirmation_login.html", {
        'email': email,
        'token': token,
        'is_new': is_new,
        'needs_confirmation': needs_confirmation,
        'next': next_url
    })

@login_required
def customlogout(request):
    logout(request)
    messages.info(request, "You have successfully logged out.")
    return render(request, "logout.html")

@never_cache
def user_settings(request):
    profile, created = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        profile.notify_new_manuscripts = request.POST.get("notify_new_manuscripts") == "on"
        profile.save()
        return redirect(reverse("optimap:usersettings"))
    return render(request, "user_settings.html", {
        "profile": profile,
        "delete_token": request.session.get(USER_DELETE_TOKEN_PREFIX, None),
    })

@login_required
def user_subscriptions(request):
    """Display and manage user's regional subscriptions."""
    user = request.user

    # Get or create the user's subscription
    subscription, created = Subscription.objects.get_or_create(
        user=user,
        defaults={'name': f'{user.username}_subscription'}
    )

    # Get all available regions, grouped by type
    continents = GlobalRegion.objects.filter(region_type=GlobalRegion.CONTINENT).order_by('name')
    oceans = GlobalRegion.objects.filter(region_type=GlobalRegion.OCEAN).order_by('name')

    # Get user's currently selected regions
    selected_region_ids = list(subscription.regions.values_list('id', flat=True))

    context = {
        'subscription': subscription,
        'continents': continents,
        'oceans': oceans,
        'selected_region_ids': selected_region_ids,
    }

    return render(request, 'subscriptions.html', context)

@login_required
def add_subscriptions(request):
    """Update user's regional subscriptions."""
    if request.method == "POST":
        user = request.user

        # Get or create the user's subscription
        subscription, created = Subscription.objects.get_or_create(
            user=user,
            defaults={'name': f'{user.username}_subscription'}
        )

        # Get selected region IDs from the form
        selected_region_ids = request.POST.getlist('regions')

        # Update the subscription's regions
        subscription.regions.clear()
        if selected_region_ids:
            regions = GlobalRegion.objects.filter(id__in=selected_region_ids)
            subscription.regions.set(regions)

        logger.info('Updated subscription for user %s with %d regions', user.username, len(selected_region_ids))
        messages.success(request, f'Subscription updated! Monitoring {len(selected_region_ids)} regions.')

        return HttpResponseRedirect('/subscriptions/')

    return HttpResponseRedirect('/subscriptions/')

@login_required
def unsubscribe(request):
    """Handles unsubscription requests from emails."""
    user = request.user
    search_term = request.GET.get("search")
    unsubscribe_all = request.GET.get("all")

    if unsubscribe_all:
        Subscription.objects.filter(user=user).update(subscribed=False)
        messages.success(request, "You have been unsubscribed from all subscriptions.")
        return redirect("/")
    if search_term:
        exact_search_term = unquote(search_term).strip()
        subscription = get_object_or_404(Subscription, user=user, search_term=exact_search_term)
        if not subscription:
            messages.warning(request, f"No subscription found for '{search_term}'.")
            return redirect("/")
        subscription.subscribed = False
        subscription.save()
        messages.success(request, f"You have unsubscribed from '{search_term}'.")
        return redirect("/")

    return HttpResponse("Invalid request.", status=400)

def delete_account(request):
    email = request.user.email
    logger.info('Delete account for %s', email)
    User.objects.filter(email=email).delete()
    messages.info(request, 'Your account has been successfully deleted.')
    return render(request, 'deleteaccount.html')

@login_required
def change_useremail(request):
    email_new = request.POST.get('email_new', False)
    currentuser = request.user
    email_old = currentuser.email
    if is_email_blocked(email_new):
        logger.warning('Attempted login with blocked email: %s', email_new)
        return render(request, "error.html", {
            'error': {
                'class': 'danger',
                'title': 'Login failed!',
                'text': 'You attempted to change your email to an address that is blocked. Please contact support for assistance.'
            }
        })
    if not email_new or email_new == email_old:
        messages.error(request, "Invalid email change request.")
        return render(request, "error.html", {
            'error': {
                'class': 'danger',
                'title': 'Invalid Email Change!',
                'text': 'You attempted to change your email to an address that is invalid. Please enter a valid email address that is different from the current one.'
            }
        })
    if User.objects.filter(email=email_new).exists():
        messages.error(request, "This email is already in use.")
        return render(request, "error.html", {
            'error': {
                'class': 'danger',
                'title': 'Email Already In Use!',
                'text': 'You attempted to change your email to an address that is already in use.'
            }
        })

    token = secrets.token_urlsafe(32)
    cache.set(
        f"{EMAIL_CONFIRMATION_TOKEN_PREFIX}_{email_new}",
        {"token": token, "old_email": request.user.email},
        timeout=EMAIL_CONFIRMATION_TIMEOUT_SECONDS,
    )
    confirm_url = request.build_absolute_uri(
        reverse("optimap:confirm_email_change", args=[token, email_new])
    )
    subject = 'Confirm Your Email Change'
    message = f"""Hello,

You requested to change your email from {email_old} to {email_new}.
Please confirm the new email by clicking on this link:

{confirm_url}

This link will expire in 10 minutes.

Thank you for using OPTIMAP!
"""
    send_mail(subject, message, settings.EMAIL_HOST_USER, [email_new])
    messages.info(request, "A confirmation email has been sent.")
    logout(request)
    return render(request, 'changeuser.html')

def confirm_email_change(request, token, email_new):
    cached_data = cache.get(f"{EMAIL_CONFIRMATION_TOKEN_PREFIX}_{email_new}")
    if not cached_data:
        messages.error(request, "Invalid or expired confirmation link.")
        return HttpResponseRedirect("/")
    if isinstance(cached_data, str):
        messages.error(request, "Cache error: Expected dictionary, got string.")
        return HttpResponseRedirect("/")
    stored_token = cached_data.get("token")
    old_email = cached_data.get("old_email")
    if stored_token != token:
        messages.error(request, "Invalid or expired confirmation link.")
        return HttpResponseRedirect("/")
    user = User.objects.filter(email=old_email).first()
    if not user:
        messages.error(request, "User not found.")
        return HttpResponseRedirect("/")
    user.email = email_new
    user.username = email_new
    user.save()
    contactURL = f"{settings.BASE_URL}/contact"
    notify_subject = 'Your OPTIMAP Email Was Changed'
    notify_message = f"""Hello,

Your email associated with OPTIMAP was changed from {old_email} to {email_new}.
If you did NOT request this change, please contact us immediately at {contactURL}.

Thank you for using OPTIMAP!
"""
    send_mail(
        notify_subject,
        notify_message,
        from_email=settings.EMAIL_HOST_USER,
        recipient_list=[old_email]
    )
    cache.delete(f"email_confirmation_{email_new}")
    login_user(request, user)
    messages.success(request, "Your email has been successfully updated!")
    return redirect("/usersettings/")

def get_login_link(request, email):
    token = secrets.token_urlsafe(nbytes=LOGIN_TOKEN_LENGTH)
    link = f"{request.scheme}://{request.site.domain}/login/{token}"

    # Store both email and next parameter in cache
    next_url = request.GET.get('next', request.POST.get('next', ''))
    cache_data = {'email': email, 'next': next_url}
    cache.set(token, cache_data, timeout=LOGIN_TOKEN_TIMEOUT_SECONDS)

    logger.info('Created login link for %s with token %s (next=%s) - %s', email, token, next_url, link)
    return link

def is_email_blocked(email):
    domain = email.split('@')[-1]
    if BlockedEmail.objects.filter(email=email).exists():
        return True
    if BlockedDomain.objects.filter(domain=domain).exists():
        return True
    return False

@login_required
def request_delete(request):
    user = request.user
    token = uuid.uuid4().hex
    cache.set(f"{USER_DELETE_TOKEN_PREFIX}_{token}", user.id, timeout=ACCOUNT_DELETE_TOKEN_TIMEOUT_SECONDS)
    confirm_url = request.build_absolute_uri(reverse('optimap:confirm_delete', args=[token]))
    timeout_minutes = ACCOUNT_DELETE_TOKEN_TIMEOUT_SECONDS // 60
    send_mail(
        'Confirm Your Account Deletion',
        f'Click the link to confirm deletion: {confirm_url}\n\n'
        f'This link is valid for {timeout_minutes} minutes. If you did not request this, ignore this email.',
        'no-reply@optimap.com',
        [user.email],
    )
    return redirect(reverse('optimap:usersettings') + '?message=Check your email for a confirmation link.')

@login_required(login_url='/')
def confirm_account_deletion(request, token):
    try:
        user_id = cache.get(f"{USER_DELETE_TOKEN_PREFIX}_{token}")
        if user_id is None:
            messages.error(request, "Invalid or expired deletion token.")
            return redirect(reverse('optimap:usersettings'))
        if request.user.id != user_id:
            messages.error(request, "You are not authorized to delete this account.")
            return redirect(reverse('optimap:main'))
        request.session[USER_DELETE_TOKEN_PREFIX] = token
        request.session.modified = True
        request.session.save()
        messages.warning(request, "Please confirm your account deletion. Your contributed data will remain on the platform.")
        return redirect(reverse('optimap:usersettings'))
    except Exception as e:
        messages.error(request, f"An error occurred: {str(e)}")
        return redirect(reverse('optimap:usersettings'))

@login_required(login_url='/')
def finalize_account_deletion(request):
    token = request.session.get(USER_DELETE_TOKEN_PREFIX)
    if not token:
        messages.error(request, "No active deletion request found.")
        return redirect(reverse('optimap:usersettings'))
    user_id = cache.get(f"{USER_DELETE_TOKEN_PREFIX}_{token}")
    if user_id is None:
        messages.error(request, "Invalid or expired deletion request.")
        return redirect(reverse('optimap:usersettings'))
    if request.user.id != user_id:
        messages.error(request, "You are not authorized to delete this account.")
        return redirect(reverse('optimap:main'))
    user = get_object_or_404(User, id=user_id)
    try:
        user.delete()
        logout(request)
        messages.success(request, "Your account has been successfully deleted.")
        return redirect(reverse('optimap:main'))
    except Exception as e:
        logger.error(f"Error deleting user {user.email}: {str(e)}")
        messages.error(request, "An error occurred while deleting your account. Please try again.")
        return redirect(reverse('optimap:usersettings'))
    finally:
        cache.delete(f"{USER_DELETE_TOKEN_PREFIX}_{token}")
        if USER_DELETE_TOKEN_PREFIX in request.session:
            del request.session[USER_DELETE_TOKEN_PREFIX]
            request.session.modified = True

def feeds(request):

    global_feeds = [
        { "title": "Geo RSS",     "url": reverse("optimap:api-feed-georss")   },
        { "title": "Atom",        "url": reverse("optimap:api-feed-atom")     },
    ]

    regions = GlobalRegion.objects.all().order_by("region_type", "name")

    # Add normalized slugs to regions for URL generation
    regions_with_slugs = []
    for region in regions:
        slug = normalize_region_slug(region.name)
        region.normalized_slug = slug
        regions_with_slugs.append(region)

    return render(request, "feeds.html", {
        "global_feeds": global_feeds,
        "regions": regions_with_slugs,
    })

def geoextent(request):
    """Geoextent extraction UI page."""
    from geoextent.lib.features import get_supported_features

    # Get supported formats and providers from geoextent's features API
    features = get_supported_features()

    # Organize file formats by handler type with display names
    supported_formats = []
    for handler in features.get('file_formats', []):
        display_name = handler.get('display_name', handler['handler'])
        extensions = [ext.lstrip('.') for ext in handler.get('file_extensions', [])]
        description = handler.get('description', '')
        if extensions:
            supported_formats.append({
                'name': display_name,
                'extensions': extensions,
                'description': description
            })

    # Extract provider details with descriptions and URLs
    supported_providers = []
    for provider in features.get('content_providers', []):
        supported_providers.append({
            'name': provider.get('name', 'Unknown'),
            'description': provider.get('description', ''),
            'website': provider.get('website', ''),
            'examples': provider.get('examples', [])
        })

    context = {
        'supported_formats': supported_formats,
        'supported_providers': supported_providers,
        'geoextent_version': features.get('version', 'unknown'),
        'max_file_size_mb': getattr(settings, 'GEOEXTENT_MAX_FILE_SIZE_MB', 100),
        'max_batch_size_mb': getattr(settings, 'GEOEXTENT_MAX_BATCH_SIZE_MB', 500),
        'max_download_size_mb': getattr(settings, 'GEOEXTENT_MAX_DOWNLOAD_SIZE_MB', 1000),
    }

    return render(request, 'geoextent.html', context)

class RobotsView(View):
    http_method_names = ['get']
    def get(self, request):
    
        # Build robots.txt content
        lines = [
            "User-Agent: *",
            "Disallow:",
            "",
            "# Sitemaps",
            f"Sitemap: {request.scheme}://{request.site.domain}/sitemap.xml",
            "",
            "# Feed URLs for indexing",
            "# Global feeds",
            f"Allow: {reverse('optimap:api-feed-georss')}",
            f"Allow: {reverse('optimap:api-feed-atom')}",
            "",
        ]

        # Add regional feeds
        regions = GlobalRegion.objects.all().order_by("region_type", "name")

        # Continents
        lines.append("# Continent feeds")
        for region in regions:
            if region.region_type == GlobalRegion.CONTINENT:
                slug = normalize_region_slug(region.name)
                lines.append(f"Allow: /feeds/continent/{slug}/")
                lines.append(f"Allow: /api/v1/feeds/continent/{slug}.rss")
                lines.append(f"Allow: /api/v1/feeds/continent/{slug}.atom")

        lines.append("")
        lines.append("# Ocean feeds")
        for region in regions:
            if region.region_type == GlobalRegion.OCEAN:
                slug = normalize_region_slug(region.name)
                lines.append(f"Allow: /feeds/ocean/{slug}/")
                lines.append(f"Allow: /api/v1/feeds/ocean/{slug}.rss")
                lines.append(f"Allow: /api/v1/feeds/ocean/{slug}.atom")

        content = "\n".join(lines)
        response = HttpResponse(content, content_type="text/plain")
        return response

def _curator_context_for(user, work):
    """Return ``(collections_list, is_curator)`` for rendering curator buttons.

    ``collections_list`` is a list of {'collection': Collection, 'is_member': bool}
    items, one per collection the user curates. The template uses ``is_member``
    to render either an Add or a Remove button.
    """
    if not user.is_authenticated:
        return [], False
    curated = list(user.curated_collections.all())
    if not curated:
        return [], False
    work_collection_ids = set(work.collections.values_list('id', flat=True))
    return (
        [{'collection': c, 'is_member': (c.id in work_collection_ids)} for c in curated],
        True,
    )


def _format_timeperiod(work):
    """
    Work stores timeperiod as arrays of strings.
    We show the first start/end if present, in a compact human form.
    """
    s_list = (work.timeperiod_startdate or [])
    e_list = (work.timeperiod_enddate   or [])
    s = s_list[0] if s_list else None
    e = e_list[0] if e_list else None

    if s and e:
        return f"{s} – {e}"
    if s:
        return f"from {s}"
    if e:
        return f"until {e}"
    return None


def _normalize_authors(work):
    """
    Try a few common attribute names. Accepts string (split on , or ;) or list/tuple.
    Returns list[str] or None.
    """
    candidates = (
        getattr(work, "authors", None),  # Primary: authors field
        getattr(work, "author", None),
        getattr(work, "creators", None),
        getattr(work, "creator", None),
    )
    raw = next((c for c in candidates if c), None)
    if not raw:
        return None
    if isinstance(raw, str):
        items = [x.strip() for x in re.split(r"[;,]", raw) if x.strip()]
        return items or None
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw if str(x).strip()]
        return items or None
    return None


def works_list(request):
    """
    Public page that lists all works with pagination:
    - DOI present  -> /work/<doi> (site-local landing page)
    - no DOI       -> fall back to Work.url (external/original)

    Only published works (status='p') are shown to non-admin users.
    Admin users see all works with status labels.

    Supports pagination with user-selectable page size.
    """
    is_admin = request.user.is_authenticated and request.user.is_staff

    # Get page size from request or use default
    page_size = request.GET.get('size', settings.WORKS_PAGE_SIZE_DEFAULT)
    try:
        page_size = int(page_size)
        # Clamp page size within allowed limits
        page_size = max(settings.WORKS_PAGE_SIZE_MIN, min(page_size, settings.WORKS_PAGE_SIZE_MAX))
    except (ValueError, TypeError):
        page_size = settings.WORKS_PAGE_SIZE_DEFAULT

    # Get page number from request
    page_number = request.GET.get('page', 1)

    # Base queryset
    if is_admin:
        pubs = Work.objects.all().select_related('source')
    else:
        pubs = Work.objects.filter(status='p').select_related('source')

    pubs = pubs.order_by("-creationDate", "-id")

    # Create paginator
    paginator = Paginator(pubs, page_size)

    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Build work data for current page
    works = []
    for work in page_obj:
        work_data = {
            "title": work.title,
            "doi": work.doi,
            "authors": work.authors or [],
            "source": work.source.name if work.source else None,
            "href": reverse("optimap:work-landing", args=[work.get_identifier()]),
        }

        # Add status info for admin users
        if is_admin:
            work_data["status"] = work.get_status_display()
            work_data["status_code"] = work.status

        works.append(work_data)

    # Get cached statistics
    stats = get_cached_statistics()

    # Build API URL for current page/size
    # DRF uses limit/offset pagination, so calculate offset from page number
    offset = (page_obj.number - 1) * page_size
    api_url = request.build_absolute_uri(
        '/api/v1/works/' +
        f'?limit={page_size}&offset={offset}'
    )

    context = {
        "works": works,
        "page_obj": page_obj,
        "page_size": page_size,
        "page_size_options": settings.WORKS_PAGE_SIZE_OPTIONS,
        "is_admin": is_admin,
        "statistics": stats,
        "api_url": api_url,
    }

    return render(request, "works.html", context)


def work_landing(request, doi):
    """
    Landing page for a work with a DOI.
    Embeds a small Leaflet map when geometry is available.

    Only published works (status='p') are accessible to non-admin users.
    Admin users can view all works with a status label.
    """
    is_admin = request.user.is_authenticated and request.user.is_staff

    # Get the work
    try:
        work = Work.objects.get(doi=doi)
    except Work.DoesNotExist:
        raise Http404("Work not found.")

    # Check access permissions
    if not is_admin and work.status != 'p':
        raise Http404("Work not found.")

    feature_json = None
    if work.geometry and not work.geometry.empty:
        feature = {
            "type": "Feature",
            "geometry": json.loads(work.geometry.geojson),
            "properties": {"title": work.title, "doi": work.doi},
        }
        feature_json = json.dumps(feature)

    # Check if geometry is missing (NULL or empty)
    has_geometry = work.geometry and not work.geometry.empty

    # Check if temporal extent is missing
    has_temporal = bool(work.timeperiod_startdate or work.timeperiod_enddate)

    # Users can contribute if work is harvested and missing either geometry or temporal extent
    can_contribute = request.user.is_authenticated and work.status == 'h' and (not has_geometry or not has_temporal)

    # Can publish if: Contributed status OR (Harvested with at least one extent type)
    can_publish = is_admin and (work.status == 'c' or (work.status == 'h' and (has_geometry or has_temporal)))
    can_unpublish = is_admin and work.status == 'p'  # Can unpublish published works

    curator_collections, is_curator = _curator_context_for(request.user, work)

    # Get most recent successful Wikidata export
    latest_wikidata_export = work.wikidata_exports.filter(
        action__in=['created', 'updated']
    ).order_by('-export_date').first()

    # Get all Wikidata exports for admin view
    all_wikidata_exports = work.wikidata_exports.all() if is_admin else []

    context = {
        "work": work,
        "feature_json": feature_json,
        "timeperiod_label": _format_timeperiod(work),
        "authors_list": _normalize_authors(work),
        "is_admin": is_admin,
        "is_curator": is_curator,
        "curator_collections": curator_collections,
        "status_display": work.get_status_display() if is_admin else None,
        "has_geometry": has_geometry,
        "has_temporal": has_temporal,
        "can_contribute": can_contribute,
        "can_publish": can_publish,
        "can_unpublish": can_unpublish,
        "show_provenance": is_admin or is_curator,
        "latest_wikidata_export": latest_wikidata_export,
        "all_wikidata_exports": all_wikidata_exports,
    }
    return render(request, "work_landing_page.html", context)


def work_landing_by_id(request, work_id):
    """
    Landing page for a work accessed by database ID.
    Used for publications without a DOI.

    Only published works (status='p') are accessible to non-admin users.
    Admin users can view all works with a status label.
    """
    is_admin = request.user.is_authenticated and request.user.is_staff

    # Get the work
    try:
        work = Work.objects.get(id=work_id)
    except Work.DoesNotExist:
        raise Http404("Work not found.")

    # Check access permissions
    if not is_admin and work.status != 'p':
        raise Http404("Work not found.")

    feature_json = None
    if work.geometry and not work.geometry.empty:
        feature = {
            "type": "Feature",
            "geometry": json.loads(work.geometry.geojson),
            "properties": {"title": work.title, "doi": work.doi},
        }
        feature_json = json.dumps(feature)

    # Check if geometry is missing (NULL or empty)
    has_geometry = work.geometry and not work.geometry.empty

    # Check if temporal extent is missing
    has_temporal = bool(work.timeperiod_startdate or work.timeperiod_enddate)

    # Users can contribute if work is harvested and missing either geometry or temporal extent
    can_contribute = request.user.is_authenticated and work.status == 'h' and (not has_geometry or not has_temporal)

    # Can publish if: Contributed status OR (Harvested with at least one extent type)
    can_publish = is_admin and (work.status == 'c' or (work.status == 'h' and (has_geometry or has_temporal)))
    can_unpublish = is_admin and work.status == 'p'  # Can unpublish published works

    curator_collections, is_curator = _curator_context_for(request.user, work)

    # Get most recent successful Wikidata export
    latest_wikidata_export = work.wikidata_exports.filter(
        action__in=['created', 'updated']
    ).order_by('-export_date').first()

    # Get all Wikidata exports for admin view
    all_wikidata_exports = work.wikidata_exports.all() if is_admin else []

    context = {
        "work": work,
        "feature_json": feature_json,
        "timeperiod_label": _format_timeperiod(work),
        "authors_list": _normalize_authors(work),
        "is_admin": is_admin,
        "is_curator": is_curator,
        "curator_collections": curator_collections,
        "status_display": work.get_status_display() if is_admin else None,
        "has_geometry": has_geometry,
        "has_temporal": has_temporal,
        "can_contribute": can_contribute,
        "can_publish": can_publish,
        "can_unpublish": can_unpublish,
        "show_provenance": is_admin or is_curator,
        "use_id_urls": True,  # Flag to use ID-based URLs in template
        "latest_wikidata_export": latest_wikidata_export,
        "all_wikidata_exports": all_wikidata_exports,
    }
    return render(request, "work_landing_page.html", context)


# ------------------------------
# Error Handlers
# ------------------------------

def custom_404(request, exception=None):
    """Custom 404 error handler"""
    return render(request, '404.html', status=404)


def custom_500(request):
    """Custom 500 error handler"""
    return render(request, '500.html', status=500)


def sitemap_page(request):
    """Human-readable sitemap page"""
    return render(request, 'sitemap_page.html')
