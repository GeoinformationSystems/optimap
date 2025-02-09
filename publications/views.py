import logging
import secrets
import json
import time
import imaplib
from datetime import datetime
from math import floor

from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse, HttpResponseRedirect
from django.core.cache import cache
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.core.mail import EmailMessage, get_connection, send_mail
from django.conf import settings
from django.utils.timezone import now

from publications.models import Subscription
from publications.forms import SubscriptionForm  # Ensure this import exists

logger = logging.getLogger(__name__)

LOGIN_TOKEN_LENGTH = 32
LOGIN_TOKEN_TIMEOUT_SECONDS = 10 * 60

# ------------------------------ General Views ------------------------------

def main(request):
    return render(request, "main.html")

def privacy(request):
    return render(request, "privacy.html")

def data(request):
    return render(request, "data.html")

def Confirmationlogin(request):
    return render(request, "confirmation_login.html")

def user_settings(request):
    return render(request, "user_settings.html")

# ------------------------------ Authentication ------------------------------

def loginres(request):
    email = request.POST.get('email', False)
    if not email:
        return JsonResponse({"error": "Email is required"}, status=400)

    subject = "OPTIMAP Login"
    link = get_login_link(request, email)
    valid = floor(LOGIN_TOKEN_TIMEOUT_SECONDS / 60)

    body = f"""Hello {email}!

You requested that we send you a link to log in to OPTIMAP at {request.site.domain}:

{link}

Please click on the link to log in.
The link is valid for {valid} minutes.
"""

    logger.info("Login process started for user %s", email)
    try:
        email_message = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.EMAIL_HOST_USER,
            to=[email],
            headers={"OPTIMAP": request.site.domain},
        )
        result = email_message.send()
        logger.info("Sent login email to %s with result: %s", email_message.recipients(), result)

        return render(request, "login_response.html", {"email": email, "valid_minutes": valid})

    except Exception as ex:
        logger.exception("Error sending login email to %s", email)
        return render(request, "error.html", {"error": {"class": "danger", "title": "Login failed!", "text": "Error sending the login email."}})

@require_GET
def authenticate_via_magic_link(request, token):
    email = cache.get(token)
    logger.info("Authenticating magic link with token %s: Found user: %s", token, email)

    if email is None:
        return render(request, "error.html", {"error": {"class": "danger", "title": "Authentication failed!", "text": "Magic link invalid or expired."}})

    user, is_new = User.objects.get_or_create(username=email, email=email)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")

    cache.delete(token)
    return render(request, "confirmation_login.html", {"is_new": is_new})

@login_required
def customlogout(request):
    logout(request)
    messages.info(request, "You have successfully logged out.")
    return render(request, "logout.html")

# ------------------------------ Subscription Management ------------------------------

@login_required
def list_subscriptions(request):
    """
    Fetch and display all subscriptions for the logged-in user.
    """
    subscriptions = Subscription.objects.filter(user=request.user)
    return render(request, "subscriptions.html", {"subscriptions": subscriptions})

@csrf_exempt
@login_required
def get_subscriptions_api(request):
    """
    API to fetch user subscriptions.
    """
    subscriptions = Subscription.objects.filter(user=request.user)
    data = [{"id": sub.id, "name": sub.name} for sub in subscriptions]
    return JsonResponse({"subscriptions": data}, safe=False)

@csrf_exempt
@login_required
def add_subscription(request):
    """
    Allow users to create a new subscription.
    """
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            name = data.get("name", "").strip()

            if not name:
                return JsonResponse({"error": "Subscription name is required"}, status=400)

            subscription = Subscription.objects.create(
                user=request.user,
                name=name,
                created_at=now(),
            )
            return JsonResponse({"message": "Subscription created successfully!", "id": subscription.id})

        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON format"}, status=400)

    return JsonResponse({"error": "Invalid request"}, status=400)

@csrf_exempt
@login_required
def edit_subscription(request, subscription_id):
    """
    Allow users to rename their subscriptions.
    """
    subscription = get_object_or_404(Subscription, id=subscription_id, user=request.user)

    if request.method == "PUT":
        try:
            data = json.loads(request.body)
            new_name = data.get("name", "").strip()

            if not new_name:
                return JsonResponse({"error": "Subscription name is required"}, status=400)

            subscription.name = new_name
            subscription.save()
            return JsonResponse({"message": "Subscription renamed successfully!"})

        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON format"}, status=400)

    return JsonResponse({"error": "Invalid request"}, status=400)

@csrf_exempt
@login_required
def delete_subscription(request, subscription_id):
    """
    Allow users to delete their subscriptions.
    """
    subscription = get_object_or_404(Subscription, id=subscription_id, user=request.user)

    if request.method == "DELETE":
        subscription.delete()
        return JsonResponse({"message": "Subscription deleted successfully!"})

    return JsonResponse({"error": "Invalid request"}, status=400)

# ------------------------------ User Account Management ------------------------------

def delete_account(request):
    """
    Allow users to delete their accounts.
    """
    email = request.user.email
    logger.info("Deleting account for %s", email)

    User.objects.filter(email=email).delete()
    messages.info(request, "Your account has been successfully deleted.")
    return render(request, "deleteaccount.html")

def change_useremail(request):
    """
    Allow users to change their email.
    """
    email_new = request.POST.get("email_new", "").strip()
    current_user = request.user
    email_old = current_user.email
    logger.info("User requests to change email from %s to %s", email_old, email_new)

    if email_new:
        current_user.email = email_new
        current_user.username = email_new
        current_user.save()

        subject = "Change Email"
        link = get_login_link(request, email_new)
        message = f"""Hello {email_new},

You requested to change your email address from {email_old} to {email_new}.
Please confirm the new email by clicking on this link:

{link}

Thank you for using OPTIMAP!
"""

        send_mail(subject, message, from_email=settings.EMAIL_HOST_USER, recipient_list=[email_new])
        logout(request)

    return render(request, "changeuser.html")

def get_login_link(request, email):
    """
    Generate a magic login link for authentication.
    """
    token = secrets.token_urlsafe(nbytes=LOGIN_TOKEN_LENGTH)
    link = f"{request.scheme}://{request.get_host()}/login/{token}"
    cache.set(token, email, timeout=LOGIN_TOKEN_TIMEOUT_SECONDS)
    logger.info("Created login link for %s with token %s - %s", email, token, link)
    return link
