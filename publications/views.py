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
from django.utils import timezone
from django.utils.timezone import now, get_default_timezone
from datetime import datetime
import imaplib
import time
from math import floor
from urllib.parse import unquote
from django.conf import settings
from django.core.serializers import serialize
from publications.models import BlockedEmail, BlockedDomain, Subscription, UserProfile, Publication
from django.contrib.auth import get_user_model
User = get_user_model()
import tempfile, os, glob
from pathlib import Path
from publications.tasks import regenerate_geojson_cache, regenerate_geopackage_cache
from osgeo import ogr, osr
ogr.UseExceptions()
import humanize

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
    layer = ds.CreateLayer("publications", srs, ogr.wkbGeometryCollection)

    for name in ("title", "abstract", "doi", "source"):
        field_defn = ogr.FieldDefn(name, ogr.OFTString)
        field_defn.SetWidth(255)
        layer.CreateField(field_defn)

    layer_defn = layer.GetLayerDefn()
    for pub in Publication.objects.all():
        feat = ogr.Feature(layer_defn)
        feat.SetField("title", pub.title or "")
        feat.SetField("abstract", pub.abstract or "")
        feat.SetField("doi", pub.doi or "")
        feat.SetField("source", pub.source.name if pub.source else "")
        if pub.geometry:
            wkb = pub.geometry.wkb
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
    return render(request, "main.html")

def about(request):
    return render(request, 'about.html')

def accessibility(request):
    return render(request, 'accessibility.html')

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

    return render(request, 'data.html', {
        'geojson_size':    geojson_size,
        'geopackage_size': geopackage_size,
        'interval':        settings.DATA_DUMP_INTERVAL_HOURS,
        'last_updated':    last_updated,
        'last_geojson':    last_geo.name  if last_geo else None,
        'last_gpkg':       last_gpkg.name if last_gpkg else None,
    })

def confirmation_login(request):
    return render(request, 'confirmation_login.html')


def login_user(request, user):
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    user.save()

@require_GET
def authenticate_via_magic_link(request, token):
    email = cache.get(token)
    logger.info('Authenticating magic link with token %s: Found user: %s', token, email)
    if email is None:
        logger.debug('Magic link invalid for user %s', email)
        return render(request, "error.html", {
            'error': {
                'class': 'danger',
                'title': 'Authentication failed!',
                'text': 'Magic link invalid or expired. Please try again!'
            }
        })
    user = User.objects.filter(email=email).first()
    if user:
        is_new = False
        needs_confirmation = False
        login_user(request, user)
    elif request.GET.get('confirmed', None) == 'true':
        user = User.objects.create_user(username=email, email=email)
        is_new = True
        needs_confirmation = False
        login_user(request, user)
    else:
        is_new = True
        needs_confirmation = True
    
    return render(request, "confirmation_login.html", {'email': email, 'token': token, 'is_new': is_new, 'needs_confirmation': needs_confirmation})

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

def user_subscriptions(request):
    if request.user.is_authenticated:
        subs = Subscription.objects.all()
        count_subs = subs.count()
        return render(request, 'subscriptions.html', {'sub': subs, 'count': count_subs})
    else:
        return HttpResponse("Unauthorized", status=401)

def add_subscriptions(request):
    if request.method == "POST":
        search_term = request.POST.get("search", False)
        start_date = request.POST.get('start_date', False)
        end_date = request.POST.get('end_date', False)
        currentuser = request.user
        user_name = currentuser.username if currentuser.is_authenticated else None
        start_date_object = datetime.strptime(start_date, '%m/%d/%Y')
        end_date_object = datetime.strptime(end_date, '%m/%d/%Y')

        subscription = Subscription(
            search_term=search_term,
            timeperiod_startdate=start_date_object,
            timeperiod_enddate=end_date_object,
            user_name=user_name
        )
        logger.info('Adding new subscription for user %s: %s', user_name, subscription)
        subscription.save()
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
        messages.error(request, "Invalid email change request.")
        return render(request, 'changeuser.html')
    if not email_new or email_new == email_old:
        messages.error(request, "Invalid email change request.")
        return render(request, 'changeuser.html')
    if User.objects.filter(email=email_new).exists():
        messages.error(request, "This email is already in use.")
        return render(request, 'changeuser.html')
    token = secrets.token_urlsafe(32)
    cache.set(
        f"{EMAIL_CONFIRMATION_TOKEN_PREFIX}_{email_new}",
        {"token": token, "old_email": request.user.email},
        timeout=EMAIL_CONFIRMATION_TIMEOUT_SECONDS,
    )
    confirm_url = request.build_absolute_uri(
        reverse("optimap:confirm-email-change", args=[token, email_new])
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
    cache.set(token, email, timeout=LOGIN_TOKEN_TIMEOUT_SECONDS)
    logger.info('Created login link for %s with token %s - %s', email, token, link)
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

class RobotsView(View):
    http_method_names = ['get']
    def get(self, request):
        response = HttpResponse(
            "User-Agent: *\nDisallow:\nSitemap: %s://%s/sitemap.xml" % (request.scheme, request.site.domain),
            content_type="text/plain"
        )
        return response
