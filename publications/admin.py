from django.contrib import admin, messages
from leaflet.admin import LeafletGeoAdmin
from django.contrib.auth.models import User
from publications.models import Publication, BlockedEmail, BlockedDomain
from import_export.admin import ImportExportModelAdmin
from django_q.tasks import schedule
from django.utils.timezone import now
from publications.models import EmailLog, Subscription, UserProfile
from publications.tasks import send_monthly_email, schedule_monthly_email_task
from django_q.models import Schedule
from datetime import datetime, timedelta

# Unregister the default User admin
admin.site.unregister(User)

@admin.action(description="Mark selected publications as published")
def make_public(modeladmin, request, queryset):
    queryset.update(status="p")

@admin.action(description="Mark selected publications as draft (unpublished)")
def make_draft(modeladmin, request, queryset):
    queryset.update(status="d")

@admin.action(description="Send Monthly Manuscript Email")
def trigger_monthly_email(modeladmin, request, queryset):
    """
    Admin action to trigger the email task manually.
    """
    try:
        send_monthly_email(trigger_source='admin', sent_by=request.user) 
        messages.success(request, "Monthly manuscript email has been sent successfully.")
    except Exception as e:
        messages.error(request, f"Failed to send email: {e}")

@admin.action(description="Schedule Monthly Email Task")
def trigger_monthly_email_task(modeladmin, request, queryset):
    """
    Admin action to manually schedule the email task.
    """
    try:        
        schedule_monthly_email_task(sent_by=request.user)  
        messages.success(request, "Monthly email task has been scheduled successfully.")
    except Exception as e:
        messages.error(request, f"Failed to schedule task: {e}")

@admin.action(description="Send subscription-based emails")
def send_subscription_emails(modeladmin, request, queryset):
    """
    Admin action to manually send subscription-based emails to selected users.
    """
    from publications.tasks import send_subscription_based_email

    selected_users = queryset.filter(subscribed=True).values_list('user', flat=True)
    if not selected_users:
        messages.warning(request, "No active subscribers selected.")
        return

    send_subscription_based_email(sent_by=request.user, user_ids=list(selected_users))
    messages.success(request, "Subscription-based emails have been sent.")

@admin.action(description="Schedule subscription-based Email Task")
def send_subscription_emails_scheduler(modeladmin, request, queryset):
    """
    Admin action to manually schedule the email task.
    """
    try:        
        schedule_subscription_email_task(sent_by=request.user)  
        messages.success(request, "Monthly email task has been scheduled successfully.")
    except Exception as e:
        messages.error(request, f"Failed to schedule task: {e}")


@admin.action(description="Delete user and block email")
def block_email(modeladmin, request, queryset):
    for user in queryset:
        BlockedEmail.objects.create(email=user.email) 
        user.delete()
    modeladmin.message_user(request, "Selected users have been deleted and their emails blocked.")

@admin.action(description="Delete user and block email and domain")
def block_email_and_domain(modeladmin, request, queryset):
    for user in queryset:
        domain = user.email.split("@")[-1]
        BlockedEmail.objects.create(email=user.email)  
        BlockedDomain.objects.get_or_create(domain=domain)  
        user.delete()
    modeladmin.message_user(request, "Selected users have been deleted and their emails/domains blocked.")

@admin.register(Publication)
class PublicationAdmin(LeafletGeoAdmin, ImportExportModelAdmin):
    """Publication Admin."""

    list_display = ("doi", "creationDate", "lastUpdate", "created_by", "updated_by", "status", "provenance")

    actions = [make_public,make_draft]

class EmailLogAdmin(admin.ModelAdmin):
    list_display = (
        "recipient_email",
        "subject",
        "sent_at",
        "sent_by",
        "trigger_source",
        "status",  
        "error_message", 
    )
    list_filter = ("status", "trigger_source", "sent_at")  
    search_fields = ("recipient_email", "subject", "sent_by__username")  
    actions = [trigger_monthly_email, trigger_monthly_email_task]  

class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "region", "subscribed")
    actions = [send_subscription_emails, send_subscription_emails_scheduler]

class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "notify_new_manuscripts")  
    search_fields = ("user__email",)

@admin.register(BlockedEmail)
class BlockedEmailAdmin(admin.ModelAdmin):
    list_display = ('email', 'created_at', 'blocked_by')
    search_fields = ('email',)

@admin.register(BlockedDomain)
class BlockedDomainAdmin(admin.ModelAdmin):
    list_display = ('domain', 'created_at', 'blocked_by')
    search_fields = ('domain',)

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    """User Admin."""
    list_display = ("username", "email", "is_active")
    actions = [block_email, block_email_and_domain]

admin.site.register(EmailLog, EmailLogAdmin)
admin.site.register(UserProfile, UserProfileAdmin)
admin.site.register(Subscription, SubscriptionAdmin)  
