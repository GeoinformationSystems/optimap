import logging
logger = logging.getLogger(__name__)

from django.contrib.auth import login, logout
from django.shortcuts import render, redirect, get_object_or_404
from django.core.cache import cache
from django.http.request import HttpRequest
from django.http import HttpResponseRedirect, HttpResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET
from django.core.mail import EmailMessage, send_mail, get_connection
from django.views.generic import View
import secrets
from django.contrib import messages
from django.views.decorators.cache import never_cache
from django.urls import reverse
import uuid
from django.utils.timezone import now
from datetime import datetime
import imaplib
import time
from math import floor
from django_currentuser.middleware import (get_current_user, get_current_authenticated_user)
from django.urls import reverse  
from django.core.serializers import serialize
from django.conf import settings
from publications.models import BlockedEmail, BlockedDomain, Subscription, UserProfile, Publication
from django.contrib.auth import get_user_model
User = get_user_model()

LOGIN_TOKEN_LENGTH  = 32
LOGIN_TOKEN_TIMEOUT_SECONDS = 10 * 60
EMAIL_CONFIRMATION_TIMEOUT_SECONDS = 10 * 60
ACCOUNT_DELETE_TOKEN_TIMEOUT_SECONDS = 10 * 60
USER_DELETE_TOKEN_PREFIX = "user_delete_token" 

# -----------------------------------------------------------------------------
# Helper Functions

def format_file_size(num_bytes):
    """
    Convert a file size in bytes into a human-readable string.
    """
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.2f} KB"
    else:
        return f"{num_bytes / (1024 * 1024):.2f} MB"

def generate_geopackage():
    """
    Generates a GeoPackage file from Publication data using GDAL/OGR.
    This creates a real GeoPackage with a layer named 'publications'
    containing fields for title, abstract, doi, and source.
    The file is written to a temporary file, read into memory, and then deleted.
    """
    from osgeo import ogr, osr
    import tempfile, os

    # Create a temporary file for the GeoPackage.
    temp_file = tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False)
    filename = temp_file.name
    temp_file.close()

    # Get the GeoPackage driver.
    driver = ogr.GetDriverByName("GPKG")
    if driver is None:
        logger.error("GeoPackage driver not available.")
        return b""

    # Create a new datasource.
    datasource = driver.CreateDataSource(filename)
    if datasource is None:
        logger.error("Could not create GeoPackage datasource.")
        return b""

    # Create spatial reference for EPSG:4326.
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)

    # Create a new layer. Using ogr.wkbUnknown allows any geometry type.
    layer = datasource.CreateLayer("publications", srs, geom_type=ogr.wkbUnknown)
    if layer is None:
        logger.error("Failed to create layer in GeoPackage.")
        return b""

    # Create fields for publication attributes.
    for field_name, field_width in (("title", 255), ("abstract", 1024), ("doi", 255), ("source", 4096)):
        field_defn = ogr.FieldDefn(field_name, ogr.OFTString)
        field_defn.SetWidth(field_width)
        ret = layer.CreateField(field_defn)
        if ret != 0:
            logger.error("Failed to create field %s", field_name)

    # Add each Publication as a feature.
    for pub in Publication.objects.all():
        feature_defn = layer.GetLayerDefn()
        feature = ogr.Feature(feature_defn)
        feature.SetField("title", pub.title)
        feature.SetField("abstract", pub.abstract if pub.abstract else "")
        feature.SetField("doi", pub.doi if pub.doi else "")
        feature.SetField("source", pub.source if pub.source else "")
        
        # Convert Django geometry to OGR geometry.
        if pub.geometry:
            try:
                ogr_geom = ogr.CreateGeometryFromWkt(pub.geometry.wkt)
                feature.SetGeometry(ogr_geom)
            except Exception as e:
                logger.error("Failed to convert geometry for publication %s: %s", pub.id, e)
                feature.SetGeometry(None)
        else:
            feature.SetGeometry(None)
        ret = layer.CreateFeature(feature)
        if ret != 0:
            logger.error("Failed to create feature for publication %s", pub.id)
        feature = None

    datasource = None  # Closes datasource and flushes data.

    # Read the generated GeoPackage file content.
    with open(filename, "rb") as f:
        geopackage_data = f.read()

    os.remove(filename)
    return geopackage_data

# -----------------------------------------------------------------------------
# Views

def main(request):
    return render(request, "main.html")

def loginres(request):
    email = request.POST.get('email', False)
    if is_email_blocked(email):
        logger.warning('Attempted login with blocked email: %s', email)
        return render(request, "error.html", {
            'error': {
                'class': 'danger',
                'title': 'Login failed!',
                'text': 'You attempted to login using an email that is blocked. Please contact support for assistance.'
            }
        })

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
        if str(get_connection().__class__.__module__).endswith("smtp"):
            with imaplib.IMAP4_SSL(settings.EMAIL_HOST_IMAP, port=settings.EMAIL_PORT_IMAP) as imap:
                message = str(email_message.message()).encode()
                imap.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
                folder = settings.EMAIL_IMAP_SENT_FOLDER
                imap.append(folder, '\\Seen', imaplib.Time2Internaldate(time.time()), message)
                logger.debug('Saved email to IMAP folder {folder}')
        return render(request, 'login_response.html', {
            'email': email,
            'valid_minutes': valid,
        })
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

def privacy(request):
    return render(request, 'privacy.html')

def data(request):
    # Generate GeoJSON content and compute its file size.
    geojson_content = serialize("geojson", Publication.objects.all())
    geojson_size = format_file_size(len(geojson_content))
    
    # Generate actual GeoPackage content using GDAL/OGR and compute its file size.
    geopackage_content = generate_geopackage()
    geopackage_size = format_file_size(len(geopackage_content))
    
    context = {
        'geojson_size': geojson_size,
        'geopackage_size': geopackage_size,
    }
    return render(request, 'data.html', context)

def Confirmationlogin(request):
    return render(request, 'confirmation_login.html')

def login_user(request, user):
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    user.save()

@require_GET
def authenticate_via_magic_link(request: HttpRequest, token: str):
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
        if user.deleted:
            user.deleted = False
            user.deleted_at = None
            user.is_active = True  
            user.save()
            is_new = False  
        else:
            is_new = False  
    else:
        user = User.objects.create_user(username=email, email=email)
        is_new = True
    login_user(request, user)
    cache.delete(token)
    return render(request, "confirmation_login.html", {'is_new': is_new})

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
        pass

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
        f"email_confirmation_{email_new}",
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
    cached_data = cache.get(f"email_confirmation_{email_new}")
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
    if user.deleted:
        messages.warning(request, "This account has already been deleted.")
        return redirect(reverse('optimap:usersettings'))
    try:
        user.deleted = True
        user.deleted_at = now()
        user.save()
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

# New Functionality: Download all geometries and metadata as GeoJSON
@require_GET
def download_geojson(request):
    """
    Serializes all Publication objects into GeoJSON format
    and returns it as a downloadable file.
    """
    geojson_data = serialize("geojson", Publication.objects.all())
    response = HttpResponse(geojson_data, content_type="application/json")
    response['Content-Disposition'] = 'attachment; filename="publications.geojson"'
    return response

# New Functionality: Download as GeoPackage (concrete implementation)
@require_GET
def download_geopackage(request):
    """
    Generates a GeoPackage file from Publication data and returns it as a downloadable file.
    Uses GDAL/OGR to create a real GeoPackage file.
    """
    geopackage_data = generate_geopackage()
    response = HttpResponse(geopackage_data, content_type="application/octet-stream")
    response['Content-Disposition'] = 'attachment; filename="publications.gpkg"'
    return response

def generate_geopackage():
    """
    Generates a GeoPackage file from Publication data using GDAL/OGR.
    This creates a real GeoPackage with a layer named 'publications'
    containing fields for title, abstract, doi, and source.
    The file is written to a temporary file, read into memory, and then deleted.
    """
    from osgeo import ogr, osr
    import tempfile, os

    # Generate a temporary file name without creating the file.
    filename = tempfile.mktemp(suffix=".gpkg")
    
    # Get the GeoPackage driver.
    driver = ogr.GetDriverByName("GPKG")
    if driver is None:
        logger.error("GeoPackage driver not available.")
        return b""

    # Create a new datasource. Since the file doesn't exist, GDAL can create it.
    datasource = driver.CreateDataSource(filename)
    if datasource is None:
        logger.error("Could not create GeoPackage datasource.")
        return b""

    # Create spatial reference for EPSG:4326.
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)

    # Create a new layer. Using ogr.wkbUnknown allows any geometry type.
    layer = datasource.CreateLayer("publications", srs, geom_type=ogr.wkbUnknown)
    if layer is None:
        logger.error("Failed to create layer in GeoPackage.")
        return b""

    # Create fields for publication attributes.
    for field_name, field_width in (("title", 255), ("abstract", 1024), ("doi", 255), ("source", 4096)):
        field_defn = ogr.FieldDefn(field_name, ogr.OFTString)
        field_defn.SetWidth(field_width)
        ret = layer.CreateField(field_defn)
        if ret != 0:
            logger.error("Failed to create field %s", field_name)

    # Add each Publication as a feature.
    for pub in Publication.objects.all():
        feature_defn = layer.GetLayerDefn()
        feature = ogr.Feature(feature_defn)
        feature.SetField("title", pub.title)
        feature.SetField("abstract", pub.abstract if pub.abstract else "")
        feature.SetField("doi", pub.doi if pub.doi else "")
        feature.SetField("source", pub.source if pub.source else "")
        
        # Convert Django geometry to OGR geometry.
        if pub.geometry:
            try:
                ogr_geom = ogr.CreateGeometryFromWkt(pub.geometry.wkt)
                feature.SetGeometry(ogr_geom)
            except Exception as e:
                logger.error("Failed to convert geometry for publication %s: %s", pub.id, e)
                feature.SetGeometry(None)
        else:
            feature.SetGeometry(None)
        ret = layer.CreateFeature(feature)
        if ret != 0:
            logger.error("Failed to create feature for publication %s", pub.id)
        feature = None

    datasource = None  # Close datasource and flush data.

    # Read the generated GeoPackage file content.
    with open(filename, "rb") as f:
        geopackage_data = f.read()

    os.remove(filename)
    return geopackage_data
    """
    Generates a GeoPackage file from Publication data using GDAL/OGR.
    This creates a real GeoPackage with a layer named 'publications'
    containing fields for title, abstract, doi, and source.
    The file is written to a temporary file, read into memory, and then deleted.
    """
    from osgeo import ogr, osr
    import tempfile, os

    # Create a temporary file for the GeoPackage.
    temp_file = tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False)
    filename = temp_file.name
    temp_file.close()

    # Get the GeoPackage driver.
    driver = ogr.GetDriverByName("GPKG")
    if driver is None:
        logger.error("GeoPackage driver not available.")
        return b""

    # Create a new datasource.
    datasource = driver.CreateDataSource(filename)
    if datasource is None:
        logger.error("Could not create GeoPackage datasource.")
        return b""

    # Create spatial reference for EPSG:4326.
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)

    # Create a new layer. Using ogr.wkbUnknown allows any geometry type.
    layer = datasource.CreateLayer("publications", srs, geom_type=ogr.wkbUnknown)
    if layer is None:
        logger.error("Failed to create layer in GeoPackage.")
        return b""

    # Create fields for publication attributes.
    for field_name, field_width in (("title", 255), ("abstract", 1024), ("doi", 255), ("source", 4096)):
        field_defn = ogr.FieldDefn(field_name, ogr.OFTString)
        field_defn.SetWidth(field_width)
        ret = layer.CreateField(field_defn)
        if ret != 0:
            logger.error("Failed to create field %s", field_name)

    # Add each Publication as a feature.
    for pub in Publication.objects.all():
        feature_defn = layer.GetLayerDefn()
        feature = ogr.Feature(feature_defn)
        feature.SetField("title", pub.title)
        feature.SetField("abstract", pub.abstract if pub.abstract else "")
        feature.SetField("doi", pub.doi if pub.doi else "")
        feature.SetField("source", pub.source if pub.source else "")
        
        # Convert Django geometry to OGR geometry.
        if pub.geometry:
            try:
                ogr_geom = ogr.CreateGeometryFromWkt(pub.geometry.wkt)
                feature.SetGeometry(ogr_geom)
            except Exception as e:
                logger.error("Failed to convert geometry for publication %s: %s", pub.id, e)
                feature.SetGeometry(None)
        else:
            feature.SetGeometry(None)
        ret = layer.CreateFeature(feature)
        if ret != 0:
            logger.error("Failed to create feature for publication %s", pub.id)
        feature = None

    datasource = None  # Closes datasource and flushes data.

    # Read the generated GeoPackage file content.
    with open(filename, "rb") as f:
        geopackage_data = f.read()

    os.remove(filename)
    return geopackage_data

def data(request):
    geojson_content = serialize("geojson", Publication.objects.all())
    geojson_size = format_file_size(len(geojson_content))
    
    geopackage_content = generate_geopackage()
    geopackage_size = format_file_size(len(geopackage_content))
    
    context = {
        'geojson_size': geojson_size,
        'geopackage_size': geopackage_size,
    }
    return render(request, 'data.html', context)

class RobotsView(View):
    http_method_names = ['get']
    def get(self, request):
        response = HttpResponse("User-Agent: *\nDisallow:\nSitemap: %s://%s/sitemap.xml" % (request.scheme, request.site.domain),
                                content_type="text/plain")
        return response
