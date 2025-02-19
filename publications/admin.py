from django.contrib import admin
from leaflet.admin import LeafletGeoAdmin
from django.contrib.auth.models import User
from publications.models import Publication, BlockedEmail, BlockedDomain
from import_export.admin import ImportExportModelAdmin

# Unregister the default User admin
admin.site.unregister(User)

@admin.action(description="Mark selected publications as published")
def make_public(modeladmin, request, queryset):
    queryset.update(status="p")

@admin.action(description="Mark selected publications as draft (unpublished)")
def make_draft(modeladmin, request, queryset):
    queryset.update(status="d")

@admin.action(description="Delete user and block email/domain")
def delete_user_and_block(modeladmin, request, queryset):
    for user in queryset:
        email = user.email
        domain = email.split('@')[-1]

        BlockedEmail.objects.get_or_create(email=email) # Add email to BlockedEmail table
        BlockedDomain.objects.get_or_create(domain=domain) # Add domain to BlockedDomain table

        # Delete user
        user.delete()

@admin.register(Publication)
class PublicationAdmin(LeafletGeoAdmin, ImportExportModelAdmin):
    """Publication Admin."""

    list_display = ("doi", "creationDate", "lastUpdate", "created_by", "updated_by", "status", "provenance")

    actions = [make_public,make_draft]

@admin.register(BlockedEmail)
class BlockedEmailAdmin(admin.ModelAdmin):
    list_display = ('email', 'created_at')
    search_fields = ('email',)

@admin.register(BlockedDomain)
class BlockedDomainAdmin(admin.ModelAdmin):
    list_display = ('domain', 'created_at')
    search_fields = ('domain',)

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    """User Admin."""
    list_display = ("username", "email", "is_active")
    actions = [delete_user_and_block]
