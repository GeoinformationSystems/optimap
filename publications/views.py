import logging
logger = logging.getLogger(__name__)

from django.contrib.auth.models import User
from django.contrib.auth import login
from django.shortcuts import render
from django.core.cache import cache
from django.http.request import HttpRequest
from django.http import HttpResponseRedirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET
from django.core.mail import EmailMessage, send_mail, get_connection
import secrets
from django.contrib import messages
from django.contrib.auth import login,logout
from django.views.decorators.http import require_GET
from django.conf import settings
from publications.models import Subscription
from datetime import datetime
import imaplib
import time
from math import floor
from django_currentuser.middleware import (get_current_user, get_current_authenticated_user)
from django.urls import reverse
import uuid
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.utils.timezone import now
from django.contrib.auth import get_user_model
User = get_user_model()

LOGIN_TOKEN_LENGTH  = 32
LOGIN_TOKEN_TIMEOUT_SECONDS = 10 * 60


def main(request):
    return render(request,"main.html")

def loginres(request):
    email = request.POST.get('email', False)
    subject = 'OPTIMAP Login'
    link = get_login_link(request, email)
    valid = floor(LOGIN_TOKEN_TIMEOUT_SECONDS / 60)
    body = f"""Hello {email} !

You requested that we send you a link to log in to OPTIMAP at {request.site.domain}:

{link}

Please click on the link to log in.
The link is valid for {valid} minutes.
"""

    logging.info('Login process started for user %s', email)
    try:
        email_message = EmailMessage(
            subject = subject,
            body = body,
            from_email = settings.EMAIL_HOST_USER,
            to = [email],
            headers={'OPTIMAP': request.site.domain}
            )
        result = email_message.send()
        logging.info('%s sent login email to %s with the result: %s', settings.EMAIL_HOST_USER, email_message.recipients(), result)
        
        # If backend is SMTP, then put the sent email into the configured folder, see also https://stackoverflow.com/a/59735890/261210
        if str(get_connection().__class__.__module__).endswith("smtp"):
            with imaplib.IMAP4_SSL(settings.EMAIL_HOST_IMAP, port = settings.EMAIL_PORT_IMAP) as imap:
                message = str(email_message.message()).encode()
                imap.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
                # must make sure the folder exists
                folder = settings.EMAIL_IMAP_SENT_FOLDER
                imap.append(folder, '\\Seen', imaplib.Time2Internaldate(time.time()), message)
                logging.debug('Saved email to IMAP folder {folder}')
                
        return render(request,'login_response.html', {
            'email': email,
            'valid_minutes': valid,
        })
    except Exception as ex:
        logging.exception('Error sending login email to %s from %s', email, settings.EMAIL_HOST_USER)
        logging.error(ex)
        return render(request, "error.html", {
            'error': {
                'class': 'danger',
                'title': 'Login failed!',
                'text': 'Error sending the login email. Please try again or contact us!'
            }
        })

def privacy(request):
    return render(request,'privacy.html')

def data(request):
    return render(request,'data.html')

def Confirmationlogin(request):
    return render(request,'confirmation_login.html')

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
            # Create a new user if existing record is found
            user.deleted = False
            user.deleted_at = None
            user.is_active = True  
            user.save()
            is_new = False  
        else:
            is_new = False  
    else:
        # Create a new user if no existing record is found
        user = User.objects.create_user(username=email, email=email)
        is_new = True

    login(request, user, backend='django.contrib.auth.backends.ModelBackend')

    cache.delete(token)
    return render(request, "confirmation_login.html", {
        'is_new': is_new
    })

@login_required
def customlogout(request):
    logout(request)
    messages.info(request, "You have successfully logged out.")
    return render(request, "logout.html")

def user_settings(request):
    return render(request,'user_settings.html')

def user_subscriptions(request):
    if request.user.is_authenticated:
        subs = Subscription.objects.all()
        count_subs = Subscription.objects.all().count()
        return render(request,'subscriptions.html',{'sub':subs,'count':count_subs})
    else:
        pass

def add_subscriptions(request):
    if request.method == "POST":
        search_term = request.POST.get("search", False)
        start_date = request.POST.get('start_date', False)
        end_date = request.POST.get('end_date', False)
        currentuser = request.user
        if currentuser.is_authenticated:            
            user_name = currentuser.username
        else : 
            user_name = None
        start_date_object = datetime.strptime(start_date, '%m/%d/%Y')
        end_date_object = datetime.strptime(end_date, '%m/%d/%Y')
        
        # save info in db
        subscription = Subscription(search_term = search_term, timeperiod_startdate = start_date_object, timeperiod_enddate = end_date_object, user_name = user_name )
        logger.info('Adding new subscription for user %s: %s', user_name, subscription)
        subscription.save()
        return  HttpResponseRedirect('/subscriptions/')

def delete_account(request):
    email = request.user.email
    logger.info('Delete account for %s', email)

    Current_user = User.objects.filter(email = email)
    Current_user.delete()
    messages.info(request, 'Your account has been successfully deleted.')
    return render(request, 'deleteaccount.html')

def change_useremail(request):
    email_new = request.POST.get('email_new', False)
    currentuser = request.user
    email_old = currentuser.email
    logger.info('User requests to change email from %s to %s', email_old, email_new)
    
    if email_new:
        currentuser.email = email_new
        currentuser.username = email_new
        currentuser.save()
        #send email
        subject = 'Change Email'
        link = get_login_link(request, email_new)
        message =f"""Hello {email_new},

You requested to change your email address from {email_old} to {email_new}.
Please confirm the new email by clicking on this link:

{link}

Thank you for using OPTIMAP!
"""
        send_mail(
            subject,
            message,
            from_email = settings.EMAIL_HOST_USER,
            recipient_list=[email_new]
        )
        logout(request)

    return render(request,'changeuser.html')

def get_login_link(request, email):
    token = secrets.token_urlsafe(nbytes = LOGIN_TOKEN_LENGTH)
    link = f"{request.scheme}://{request.site.domain}/login/{token}"
    cache.set(token, email, timeout = LOGIN_TOKEN_TIMEOUT_SECONDS)
    logger.info('Created login link for %s with token %s - %s', email, token, link)
    return link


@login_required
def request_delete(request):
    user = request.user
    token = uuid.uuid4().hex

    cache.set(f"delete_token_{token}", user.id, timeout=3600)

    confirm_url = request.build_absolute_uri(reverse('optimap:confirm_delete', args=[token]))

    send_mail(  
        'Confirm Your Account Deletion',
        f'Click the link to confirm deletion: {confirm_url}',
        'no-reply@optimap.com',
        [user.email],
    )

    return redirect(reverse('optimap:usersettings') + '?message=Check your email for a confirmation link.')

@login_required
def confirm_account_deletion(request, token):
    user_id = cache.get(f"delete_token_{token}")

    if user_id is None:
        messages.error(request, "Invalid or expired deletion token.")
        return redirect(reverse('optimap:usersettings'))

    user = get_object_or_404(User, id=user_id)

    request.session["delete_token"] = token

    messages.warning(request, "Please confirm your account deletion. Your contributed data will remain on the platform.")
    return redirect(reverse('optimap:usersettings'))  


@login_required
def finalize_account_deletion(request):
    token = request.session.get("delete_token")

    if not token:
        messages.error(request, "No active deletion request found.")
        return redirect(reverse('optimap:usersettings'))

    user_id = cache.get(f"delete_token_{token}")

    if user_id is None:
        messages.error(request, "Invalid or expired deletion request.")
        return redirect(reverse('optimap:usersettings'))

    user = get_object_or_404(User, id=user_id)

    if user.deleted:
        messages.warning(request, "This account has already been deleted.")
        return redirect(reverse('optimap:usersettings'))

    user.deleted = True
    user.deleted_at = now()
    user.save()
    cache.delete(f"delete_token_{token}") 

    if "delete_token" in request.session:
        del request.session["delete_token"]
        request.session.modified = True  

    logout(request)

    messages.success(request, "Your account has been successfully deleted.")
    return redirect(reverse('optimap:main')) 