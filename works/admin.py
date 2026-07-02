# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import logging

logger = logging.getLogger(__name__)

from django.conf import settings
from django.contrib import admin, messages
from django.core.mail import send_mail
from django.http import HttpResponse
from django.test import Client
from django.urls import reverse
from django.utils.html import format_html
from django.utils.timezone import now
from django_q.models import Schedule
from django_q.tasks import async_task
from import_export.admin import ImportExportModelAdmin
from leaflet.admin import LeafletGeoAdmin

from works.models import (
    Author,
    BaseMapLayer,
    BlockedDomain,
    BlockedEmail,
    Collection,
    Contribution,
    Country,
    CustomUser,
    EmailLog,
    GlobalRegion,
    HarvestingEvent,
    ServiceToken,
    Source,
    Subscription,
    UserProfile,
    WikidataExportLog,
    Work,
    WorkAuthor,
    WorkRelation,
)
from works.tasks import (
    regenerate_all_data_dumps,
    schedule_monthly_email_task,
    schedule_subscription_email_task,
    schedule_weekly_subscription_email_task,
    send_monthly_email,
    send_subscription_based_email,
)
from works.wikidata import export_works_to_wikidata, export_works_to_wikidata_dryrun


@admin.action(description="Export selected works to Wikidata/Wikibase")
def export_to_wikidata(modeladmin, request, queryset):
    stats = export_works_to_wikidata(queryset)

    # Success messages
    if stats["created"]:
        messages.success(request, f"{stats['created']} new Wikidata item(s) created.")
    if stats["updated"]:
        messages.success(request, f"{stats['updated']} existing Wikidata item(s) updated.")
    if stats["skipped"]:
        messages.info(request, f"{stats['skipped']} work(s) skipped (already exist or duplicate labels).")

    # Error messages
    if stats["errors"]:
        messages.error(
            request, f"{stats['errors']} work(s) failed to export. Check the Wikidata export logs for details."
        )

    # Summary message
    messages.info(request, f"Total: {stats['total']} work(s) processed.")


@admin.action(description="[DRY-RUN] Export selected works to Wikidata/Wikibase")
def export_to_wikidata_dryrun(modeladmin, request, queryset):
    stats = export_works_to_wikidata_dryrun(queryset)

    # Dry-run summary messages
    messages.info(request, "[DRY-RUN] Export simulation complete:")

    if stats["created"]:
        messages.info(request, f"  • Would create {stats['created']} new Wikidata item(s)")
    if stats["updated"]:
        messages.info(request, f"  • Would update {stats['updated']} existing Wikidata item(s)")
    if stats["skipped"]:
        messages.info(request, f"  • Would skip {stats['skipped']} work(s)")
    if stats["errors"]:
        messages.warning(request, f"  • {stats['errors']} work(s) have validation errors")

    # Summary message
    messages.success(
        request, f"[DRY-RUN] Total: {stats['total']} work(s) analyzed. No changes were written to Wikibase."
    )


@admin.action(description="Mark selected works as published")
def make_public(modeladmin, request, queryset):
    queryset.update(status="p")


@admin.action(description="Mark selected works as draft (unpublished)")
def make_draft(modeladmin, request, queryset):
    queryset.update(status="d")


def _enqueue_harvest(sources, request, modeladmin):
    user_id = request.user.id if request.user.is_authenticated else None
    queued = 0
    skipped = []
    for source in sources:
        task_func = Source.SOURCE_TYPE_TASKS.get(source.source_type)
        if not task_func:
            skipped.append((source, source.source_type))
            continue
        async_task(task_func, source.id, user_id)
        queued += 1
    if queued:
        modeladmin.message_user(
            request,
            f"Queued {queued} harvest(s); watch the HarvestingEvent admin for progress.",
            level=messages.SUCCESS,
        )
    if skipped:
        names = ", ".join(f"{s.name!r} (source_type={t!r})" for s, t in skipped)
        modeladmin.message_user(
            request,
            f"Skipped {len(skipped)} source(s) — no harvester registered for their source_type: {names}",
            level=messages.WARNING,
        )


@admin.action(description="Trigger harvesting for selected sources")
def trigger_harvesting_for_specific(modeladmin, request, queryset):
    _enqueue_harvest(queryset, request, modeladmin)


@admin.action(description="Trigger harvesting for all sources")
def trigger_harvesting_for_all(modeladmin, request, queryset):
    _enqueue_harvest(Source.objects.all(), request, modeladmin)


@admin.action(description="Schedule harvesting for selected sources")
def schedule_harvesting(modeladmin, request, queryset):
    scheduled = 0
    already_scheduled = 0
    no_task = []
    for source in queryset:
        task_func = Source.SOURCE_TYPE_TASKS.get(source.source_type)
        if not task_func:
            no_task.append((source, source.source_type))
            continue
        name = f"Manual Harvest Source {source.id}"
        if Schedule.objects.filter(name=name).exists():
            already_scheduled += 1
            continue
        Schedule.objects.create(
            func=task_func,
            args=str(source.id),
            schedule_type=Schedule.ONCE,
            next_run=now(),
            name=name,
        )
        scheduled += 1
    if scheduled:
        modeladmin.message_user(request, f"Scheduled {scheduled} one-off harvest(s).", level=messages.SUCCESS)
    if already_scheduled:
        modeladmin.message_user(
            request, f"Skipped {already_scheduled} source(s) — already scheduled.", level=messages.WARNING
        )
    if no_task:
        names = ", ".join(f"{s.name!r} (source_type={t!r})" for s, t in no_task)
        modeladmin.message_user(
            request,
            f"Skipped {len(no_task)} source(s) — no harvester registered for their source_type: {names}",
            level=messages.WARNING,
        )


@admin.action(description="Send Monthly Manuscript Email")
def trigger_monthly_email(modeladmin, request, queryset):
    """
    Admin action to trigger the email task manually.
    """
    try:
        send_monthly_email(trigger_source="admin", sent_by=request.user)
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
    selected_users = queryset.filter(subscribed=True).values_list("user", flat=True)
    if not selected_users:
        messages.warning(request, "No active subscribers selected.")
        return

    send_subscription_based_email(sent_by=request.user, user_ids=list(selected_users))
    messages.success(request, "Subscription-based emails have been sent.")


@admin.action(description="Schedule monthly subscription email task")
def send_subscription_emails_scheduler(modeladmin, request, queryset):
    """
    Admin action to manually schedule the monthly email task.
    """
    try:
        schedule_subscription_email_task(sent_by=request.user)
        messages.success(request, "Monthly subscription email task has been scheduled successfully.")
    except Exception as e:
        messages.error(request, f"Failed to schedule task: {e}")


@admin.action(description="Schedule weekly subscription email task")
def send_weekly_subscription_emails_scheduler(modeladmin, request, queryset):
    """
    Admin action to schedule the weekly subscription email task (Monday 02:00 UTC).
    """
    try:
        schedule_weekly_subscription_email_task(sent_by=request.user)
        messages.success(request, "Weekly subscription email task has been scheduled successfully.")
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


@admin.action(description="Regenerate all data exports now")
def regenerate_all_exports(modeladmin, request, queryset):
    """
    Immediately rebuild every cached data dump (GeoJSON + GeoPackage + CSV)
    in ``/tmp/optimap_cache/`` from a single PostGIS pass.
    """
    try:
        regenerate_all_data_dumps()
        messages.success(request, "GeoJSON, GeoPackage, and CSV caches were regenerated.")
    except Exception as e:
        messages.error(request, f"Error during export regeneration: {e}")


@admin.register(Work)
class WorkAdmin(LeafletGeoAdmin, ImportExportModelAdmin):
    list_display = (
        "title",
        "type",
        "doi",
        "has_permalink",
        "permalink_link",
        "creationDate",
        "lastUpdate",
        "created_by",
        "updated_by",
        "status",
        "source",
        "collections_label",
        "openalex_id",
    )
    search_fields = ("title", "doi", "abstract", "source__name", "collections__name", "openalex_id")
    list_filter = (
        "type",
        "status",
        "creationDate",
        "collections",
        "openalex_is_retracted",
        "openalex_open_access_status",
    )
    filter_horizontal = ("collections", "countries", "regions")
    fields = (
        "title",
        "type",
        "doi",
        "status",
        "source",
        "collections",
        "countries",
        "regions",
        "abstract",
        "publisher",
        "volume",
        "issue",
        "first_page",
        "last_page",
        "language",
        "geometry",
        "timeperiod_startdate",
        "timeperiod_enddate",
        "created_by",
        "updated_by",
        "provenance",
        "authors",
        "keywords",
        "topics",
        "openalex_id",
        "openalex_link",
        "openalex_match_info",
        "openalex_fulltext_origin",
        "openalex_is_retracted",
        "openalex_ids",
        "openalex_open_access_status",
        "locations",
    )
    readonly_fields = ("created_by", "updated_by", "openalex_link", "locations")
    actions = [
        make_public,
        make_draft,
        "unmerge_works",
        regenerate_all_exports,
        "export_permalinks_csv",
        "email_permalinks_preview",
        export_to_wikidata,
        export_to_wikidata_dryrun,
    ]

    @admin.action(description="Un-merge (re-promote redirected duplicates)")
    def unmerge_works(self, request, queryset):
        """Reverse an automatic dedup merge for the selected Redirected works.

        Re-promotes each ``status='r'`` tombstone to a standalone Harvested work
        and removes it from its canonical work's ``provenance.dedup`` list (see
        works.dedup.unmerge). Filter the list by status **Redirected** first.
        """
        from works.dedup import unmerge

        redirected = [w for w in queryset if w.status == "r"]
        skipped = len(queryset) - len(redirected)
        for work in redirected:
            unmerge(work)
        if redirected:
            messages.success(
                request,
                f"Re-promoted {len(redirected)} work(s) to Harvested and detached them from their canonical works.",
            )
        if skipped:
            messages.warning(
                request,
                f"Skipped {skipped} selected work(s) that are not Redirected (status='r'); only merged duplicates can be un-merged.",
            )

    @admin.display(boolean=True, description="Has DOI")
    def has_permalink(self, obj):
        return bool(obj.doi)

    @admin.display(description="Permalink")
    def permalink_link(self, obj):
        url = obj.permalink()
        return format_html('<a href="{}" target="_blank">{}</a>', url, url) if url else "—"

    @admin.display(description="Collections")
    def collections_label(self, obj):
        names = list(obj.collections.values_list("name", flat=True))
        return ", ".join(names) if names else "—"

    @admin.display(description="OpenAlex Link")
    def openalex_link(self, obj):
        if obj.openalex_id:
            return format_html(
                '<a href="{}" target="_blank"><i class="fas fa-external-link-alt"></i> View in OpenAlex</a>',
                obj.openalex_id,
            )
        return "—"

    def export_permalinks_csv(self, request, queryset):
        rows = [("title", "doi", "permalink")]
        rows += [(p.title or "", p.doi, p.permalink() or "") for p in queryset.only("title", "doi") if p.doi]
        if len(rows) == 1:
            self.message_user(request, "No items with DOI in selection.", level=messages.WARNING)
            return

        def escape_row(v):
            return '"{}"'.format((v or "").replace('"', '""'))

        csv = "\n".join(",".join(map(escape_row, r)) for r in rows)
        resp = HttpResponse(csv, content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = 'attachment; filename="publication_permalinks.csv"'
        return resp

    export_permalinks_csv.short_description = "Export permalinks (CSV)"

    def email_permalinks_preview(self, request, queryset):
        base = settings.BASE_URL.rstrip("/")
        c = Client()
        lines, ok, bad = [], 0, 0
        for p in queryset.only("title", "doi"):
            if not p.doi:
                continue
            url = p.permalink()
            path = url[len(base) :] if url and url.startswith(base) else url
            status = c.get(path).status_code
            ok += status == 200
            bad += status != 200
            lines.append(f"{'✅' if status == 200 else '❌'} {p.title} — {url} (HTTP {status})")
        if not lines:
            self.message_user(request, "No items with DOI in selection.", level=messages.WARNING)
            return
        send_mail(
            "OPTIMAP — Permalink preview",
            "Selected work permalinks:\n\n" + "\n".join(lines) + f"\n\nSummary: {ok} OK, {bad} not OK",
            settings.EMAIL_HOST_USER,
            [request.user.email],
        )
        self.message_user(request, f"Emailed preview to {request.user.email}.", level=messages.INFO)

    email_permalinks_preview.short_description = "Email permalinks preview to me"


@admin.action(description="Retry selected harvesting events")
def retry_event(modeladmin, request, queryset):
    user_id = request.user.id if request.user.is_authenticated else None
    queued = 0
    skipped = []
    for event in queryset:
        source = event.source
        task_func = Source.SOURCE_TYPE_TASKS.get(source.source_type) if source else None
        if not task_func:
            skipped.append((event, getattr(source, "source_type", None)))
            continue
        async_task(task_func, event.source_id, user_id)
        queued += 1
    if queued:
        modeladmin.message_user(
            request,
            f"Re-queued {queued} harvest(s); a new HarvestingEvent will appear per source.",
            level=messages.SUCCESS,
        )
    if skipped:
        names = ", ".join(f"event #{e.id} (source_type={t!r})" for e, t in skipped)
        modeladmin.message_user(
            request,
            f"Skipped {len(skipped)} event(s) — no harvester registered for their source_type: {names}",
            level=messages.WARNING,
        )


class RecentHarvestingEventInline(admin.TabularInline):
    model = HarvestingEvent
    extra = 0
    max_num = 0
    can_delete = False
    show_change_link = True
    fields = ("status", "started_at", "completed_at", "records_added", "records_updated", "error_message")
    readonly_fields = fields
    ordering = ("-started_at",)
    verbose_name_plural = "Recent harvesting events"

    def get_queryset(self, request):
        # Only show the 5 most recent events on the source change form.
        qs = super().get_queryset(request).order_by("-started_at")
        recent_ids = list(qs.values_list("id", flat=True)[:5])
        return qs.filter(id__in=recent_ids)

    def has_add_permission(self, request, obj=None):
        return False


class OpenAlexIdFilter(admin.SimpleListFilter):
    title = "OpenAlex ID"
    parameter_name = "has_openalex_id"

    def lookups(self, request, model_admin):
        return [
            ("yes", "Has OpenAlex ID"),
            ("no", "No OpenAlex ID"),
        ]

    def queryset(self, request, queryset):
        from django.db.models import Q

        if self.value() == "yes":
            return queryset.exclude(openalex_id__isnull=True).exclude(openalex_id="")
        if self.value() == "no":
            return queryset.filter(Q(openalex_id__isnull=True) | Q(openalex_id=""))
        return queryset


class OpenAlexStatsFilter(admin.SimpleListFilter):
    title = "OpenAlex stats"
    parameter_name = "openalex_stats"

    def lookups(self, request, model_admin):
        return [
            ("fetched", "Count fetched"),
            ("pending", "ID set, no count yet"),
            ("none", "No OpenAlex ID"),
        ]

    def queryset(self, request, queryset):
        from django.db.models import Q

        if self.value() == "fetched":
            return queryset.exclude(statistics__isnull=True).exclude(
                **{"statistics__openalex_works_count__isnull": True}
            )
        if self.value() == "pending":
            return (
                queryset.exclude(openalex_id__isnull=True)
                .exclude(openalex_id="")
                .filter(Q(statistics__isnull=True) | Q(**{"statistics__openalex_works_count__isnull": True}))
            )
        if self.value() == "none":
            return queryset.filter(Q(openalex_id__isnull=True) | Q(openalex_id=""))
        return queryset


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "landing_link",
        "source_type",
        "collection",
        "is_oa",
        "is_preprint",
        "last_harvest",
        "harvest_interval_minutes",
        "latest_event_status",
        "events_count",
        "openalex_works_count_display",
        "oai_works_count_display",
        "crossref_works_count_display",
    )
    list_filter = ("source_type", "is_oa", "is_preprint", "default_work_type", "collection", OpenAlexStatsFilter)
    search_fields = ("name", "slug", "url_field", "issn_l", "publisher_name", "openalex_id", "collection__name")
    prepopulated_fields = {"slug": ("name",)}
    actions = [trigger_harvesting_for_specific, trigger_harvesting_for_all, schedule_harvesting]
    inlines = [RecentHarvestingEventInline]
    # Grouped fieldsets so operators can find which fields belong together —
    # see docs/manage.md → "Source field cheatsheet" for what to enter where
    # per source_type.
    fieldsets = (
        (
            "Identification (mandatory)",
            {
                "fields": ("name", "slug", "source_type", "url_field"),
                "description": (
                    "<code>name</code>, <code>source_type</code>, and <code>url_field</code> "
                    "are the only mandatory fields. What to put in <code>url_field</code> "
                    "depends on the source type — see docs/manage.md. <code>slug</code> "
                    "powers the public <code>/in/&lt;slug&gt;/</code> landing page and feeds; "
                    "leave it blank to auto-generate from the name."
                ),
            },
        ),
        (
            "Harvesting configuration",
            {
                "fields": (
                    "harvest_interval_minutes",
                    "collection",
                    "default_work_type",
                ),
                "description": (
                    "<code>harvest_interval_minutes = 0</code> means manual-only. "
                    "Set a positive value to enable auto-scheduling. Leaving "
                    "<code>collection</code> blank is fine — harvesting still works."
                ),
            },
        ),
        (
            "Crossref harvesting",
            {
                "fields": ("doi_prefix", "crossref_filter", "doi_contains", "source_titles"),
                "description": (
                    "For <code>source_type=crossref-prefix</code>. <code>doi_prefix</code> is the "
                    "Crossref filter prefix (e.g. <code>10.5194</code>). <code>crossref_filter</code> "
                    "overrides the prefix with raw filter clauses (e.g. "
                    "<code>member:311,type:posted-content</code> for ESS Open Archive, whose two DOI "
                    "eras share no prefix). When a query spans multiple venues, set "
                    "<code>doi_contains</code> to keep only matching DOIs (e.g. <code>essoar</code>). "
                    "<code>source_titles</code> is an optional list of <code>container-title</code> "
                    "filter values. All optional for other source types."
                ),
            },
        ),
        (
            "OpenAlex / external identifiers",
            {
                "fields": ("openalex_id", "issn_l", "abbreviated_title"),
                "description": (
                    "For <code>source_type=openalex</code>, set <code>openalex_id</code> "
                    "to the bare <code>S&lt;id&gt;</code> (e.g. <code>S4210203054</code>) — "
                    "the harvester also accepts the <code>S&lt;id&gt;</code> embedded in "
                    "<code>url_field</code> as a fallback. The public Source API derives "
                    "<code>openalex_url</code> on the fly. Optional for other source types."
                ),
            },
        ),
        (
            "Display metadata",
            {
                "fields": ("publisher_name", "homepage_url", "is_oa", "is_preprint", "tags"),
                "description": "All optional. Display only — none of these affect harvesting.",
            },
        ),
        (
            "Statistics (auto-populated)",
            {
                "fields": ("works_count", "cited_by_count", "last_harvest", "statistics"),
                "classes": ("collapse",),
            },
        ),
    )
    readonly_fields = ("last_harvest", "statistics")

    @admin.display(description="Landing page")
    def landing_link(self, obj):
        url = obj.get_absolute_url()
        if not url:
            return "—"
        return format_html('<a href="{}" target="_blank" rel="noopener">/in/{}/</a>', url, obj.slug)

    @admin.display(description="Latest event")
    def latest_event_status(self, obj):
        latest = obj.harvesting_events.order_by("-started_at").first()
        if latest is None:
            return "—"
        url = reverse("admin:works_harvestingevent_change", args=[latest.id])
        when = latest.started_at.strftime("%Y-%m-%d %H:%M") if latest.started_at else ""
        return format_html('<a href="{}">{} ({})</a>', url, latest.status, when)

    @admin.display(description="# events")
    def events_count(self, obj):
        return obj.harvesting_events.count()

    @admin.display(description="OA works (total)")
    def openalex_works_count_display(self, obj):
        has_id = bool(obj.openalex_id and obj.openalex_id.strip())
        if not has_id:
            return format_html("<span style='color:#bbb'>no ID</span>")
        stats = obj.statistics or {}
        count = stats.get("openalex_works_count")
        if count is None:
            return format_html("<span style='color:#aaa'>pending</span>")
        fetched_at = stats.get("openalex_fetched_at", "")
        date_part = fetched_at[:10] if fetched_at else ""
        return format_html("{} <small style='color:#888'>({})</small>", f"{count:,}", date_part)

    @admin.display(description="OAI records (total)")
    def oai_works_count_display(self, obj):
        oai_types = ("oai-pmh", "ojs", "janeway")
        if obj.source_type not in oai_types:
            return format_html("<span style='color:#bbb'>n/a</span>")
        stats = obj.statistics or {}
        count = stats.get("oai_works_count")
        if count is None:
            return format_html("<span style='color:#aaa'>pending</span>")
        fetched_at = stats.get("oai_fetched_at", "")
        date_part = fetched_at[:10] if fetched_at else ""
        return format_html("{} <small style='color:#888'>({})</small>", f"{count:,}", date_part)

    @admin.display(description="Crossref works (total)")
    def crossref_works_count_display(self, obj):
        if obj.source_type != "crossref-prefix":
            return format_html("<span style='color:#bbb'>n/a</span>")
        stats = obj.statistics or {}
        count = stats.get("crossref_works_count")
        if count is None:
            return format_html("<span style='color:#aaa'>pending</span>")
        fetched_at = stats.get("crossref_fetched_at", "")
        date_part = fetched_at[:10] if fetched_at else ""
        return format_html("{} <small style='color:#888'>({})</small>", f"{count:,}", date_part)


@admin.register(HarvestingEvent)
class HarvestingEventAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source_link",
        "status",
        "started_at",
        "duration_display",
        "records_added",
        "records_updated",
        "records_with_spatial",
        "records_with_temporal",
        "error_message_short",
    )
    list_filter = ("status", "source", "started_at")
    search_fields = ("source__name", "source__url_field", "error_message", "log_text")
    date_hierarchy = "started_at"
    actions = [retry_event]
    fields = (
        "source",
        "user",
        "status",
        "started_at",
        "completed_at",
        "records_added",
        "records_updated",
        "records_with_spatial",
        "records_with_temporal",
        "error_message",
        "log_text_pretty",
    )
    readonly_fields = (
        "source",
        "user",
        "status",
        "started_at",
        "completed_at",
        "records_added",
        "records_updated",
        "records_with_spatial",
        "records_with_temporal",
        "error_message",
        "log_text_pretty",
    )

    def has_add_permission(self, request):
        return False

    @admin.display(description="Source", ordering="source__name")
    def source_link(self, obj):
        url = reverse("admin:works_source_change", args=[obj.source_id])
        return format_html('<a href="{}">{}</a>', url, obj.source.name)

    @admin.display(description="Duration")
    def duration_display(self, obj):
        if obj.completed_at and obj.started_at:
            delta = obj.completed_at - obj.started_at
            total = int(delta.total_seconds())
            if total < 60:
                return f"{total}s"
            return f"{total // 60}m {total % 60}s"
        return "—"

    @admin.display(description="Error")
    def error_message_short(self, obj):
        if not obj.error_message:
            return ""
        return (obj.error_message[:80] + "…") if len(obj.error_message) > 80 else obj.error_message

    @admin.display(description="Log")
    def log_text_pretty(self, obj):
        if not obj.log_text:
            return "—"
        return format_html(
            '<pre style="white-space: pre-wrap; max-height: 600px; overflow: auto;">{}</pre>', obj.log_text
        )


@admin.register(EmailLog)
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


@admin.register(WikidataExportLog)
class WikidataExportLogAdmin(admin.ModelAdmin):
    """Admin interface for Wikidata export logs."""

    list_display = (
        "id",
        "publication_title",
        "action",
        "wikidata_link",
        "export_date",
        "fields_count",
    )
    list_filter = ("action", "export_date")
    search_fields = (
        "publication__title",
        "publication__doi",
        "wikidata_qid",
        "export_summary",
    )
    readonly_fields = (
        "work",
        "export_date",
        "action",
        "wikidata_qid",
        "wikidata_url",
        "wikidata_link_display",
        "wikibase_endpoint",
        "exported_fields",
        "error_message_display",
        "export_summary",
    )
    fields = (
        "work",
        "export_date",
        "action",
        "wikibase_endpoint",
        "wikidata_qid",
        "wikidata_link_display",
        "export_summary",
        "exported_fields",
        "error_message_display",
    )
    ordering = ("-export_date",)
    date_hierarchy = "export_date"

    @admin.display(description="Work")
    def publication_title(self, obj):
        return obj.work.title[:60] if obj.work else "—"

    @admin.display(description="Wikidata")
    def wikidata_link(self, obj):
        if obj.wikidata_qid and obj.wikidata_url:
            return format_html(
                '<a href="{}" target="_blank" rel="noopener"><i class="fas fa-external-link-alt"></i> {}</a>',
                obj.wikidata_url,
                obj.wikidata_qid,
            )
        return "—"

    @admin.display(description="Wikidata Link")
    def wikidata_link_display(self, obj):
        if obj.wikidata_qid and obj.wikidata_url:
            return format_html(
                '<a href="{}" target="_blank" rel="noopener">{}</a>', obj.wikidata_url, obj.wikidata_url
            )
        return "—"

    @admin.display(description="Fields")
    def fields_count(self, obj):
        if obj.exported_fields:
            return len(obj.exported_fields)
        return 0

    @admin.display(description="Error Message (Full Traceback)")
    def error_message_display(self, obj):
        if obj.error_message:
            return format_html(
                '<pre style="white-space: pre-wrap; font-family: monospace; font-size: 12px; background: #f5f5f5; padding: 10px; border: 1px solid #ddd; border-radius: 4px; max-height: 400px; overflow-y: auto;">{}</pre>',
                obj.error_message,
            )
        return "—"


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "subscribed", "notification_interval", "last_notified")
    list_filter = ("subscribed", "notification_interval")
    readonly_fields = ("last_notified",)
    actions = [send_subscription_emails, send_subscription_emails_scheduler, send_weekly_subscription_emails_scheduler]


@admin.register(WorkRelation)
class WorkRelationAdmin(admin.ModelAdmin):
    list_display = ("from_work", "relation_type", "to_work", "to_identifier", "to_identifier_type", "source")
    list_filter = ("relation_type", "to_identifier_type", "source")
    search_fields = ("from_work__title", "from_work__doi", "to_work__title", "to_work__doi", "to_identifier")
    raw_id_fields = ("from_work", "to_work")
    readonly_fields = ("created_at",)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "notify_new_manuscripts", "recognition_opt_in", "recognition_username")
    list_filter = ("recognition_opt_in",)
    search_fields = ("user__email", "recognition_username")


@admin.register(Contribution)
class ContributionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "kind", "work")
    list_filter = ("kind", "created_at")
    search_fields = ("user__email", "work__title", "work__doi")
    readonly_fields = ("user", "work", "kind", "created_at")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False


@admin.register(BlockedEmail)
class BlockedEmailAdmin(admin.ModelAdmin):
    list_display = ("email", "created_at", "blocked_by")
    search_fields = ("email",)


@admin.register(BlockedDomain)
class BlockedDomainAdmin(admin.ModelAdmin):
    list_display = ("domain", "created_at", "blocked_by")
    search_fields = ("domain",)


@admin.register(CustomUser)
class UserAdmin(admin.ModelAdmin):
    """User Admin."""

    list_display = ("username", "email", "is_active")
    actions = [block_email, block_email_and_domain]


@admin.register(BaseMapLayer)
class BaseMapLayerAdmin(admin.ModelAdmin):
    """Background tile layer switcher — one row per leaflet-providers.js key."""

    list_display = ("label", "provider_key", "enabled", "is_default", "order")
    list_editable = ("enabled", "is_default", "order")
    list_display_links = ("label",)
    ordering = ("order", "label")
    search_fields = ("label", "provider_key")

    def save_model(self, request, obj, form, change):
        from django.core.cache import cache

        super().save_model(request, obj, form, change)
        # Enforce at most one is_default among enabled layers: if this row is
        # being set as default, clear the flag on all other enabled rows.
        if obj.enabled and obj.is_default:
            BaseMapLayer.objects.filter(enabled=True, is_default=True).exclude(pk=obj.pk).update(is_default=False)
        cache.delete("optimap_basemaps")

    def delete_model(self, request, obj):
        from django.core.cache import cache

        super().delete_model(request, obj)
        cache.delete("optimap_basemaps")


@admin.register(GlobalRegion)
class GlobalRegionAdmin(admin.ModelAdmin):
    """GlobalRegion Admin."""

    list_display = ("name", "region_type", "last_loaded")
    list_filter = ("region_type",)
    search_fields = ("name",)
    ordering = ("region_type", "name")


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    """Country outlines for /at/<country>/ pages and the countries map layer."""

    list_display = ("name", "iso_code", "last_loaded")
    search_fields = ("name", "iso_code")
    ordering = ("name",)


class WorkAuthorInline(admin.TabularInline):
    model = WorkAuthor
    extra = 0
    fields = ("author", "rank")
    autocomplete_fields = ("author",)
    ordering = ("rank",)


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    """Identity-bearing authors (ORCID / OpenAlex id) for /by/<orcid>/ pages."""

    list_display = ("name", "orcid", "openalex_id", "last_seen")
    search_fields = ("name", "orcid", "openalex_id")
    ordering = ("name",)
    readonly_fields = ("last_seen",)


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    """Curated grouping of Works (e.g. mountain-wetlands, agile-gi)."""

    list_display = (
        "name",
        "identifier",
        "short_slug",
        "is_published",
        "work_count",
        "curator_count",
        "source_count",
        "openalex_total_works_display",
    )
    list_filter = ("is_published",)
    search_fields = ("name", "identifier", "short_slug", "description")
    prepopulated_fields = {"identifier": ("name",)}
    filter_horizontal = ("curators",)
    fields = (
        "identifier",
        "short_slug",
        "name",
        "description",
        "homepage_url",
        "logo_url",
        "is_published",
        "curators",
        "created_at",
        "updated_at",
    )
    readonly_fields = ("created_at", "updated_at")
    actions = ["publish_collections", "unpublish_collections"]

    @admin.display(description="# works")
    def work_count(self, obj):
        return obj.works.count()

    @admin.display(description="# curators")
    def curator_count(self, obj):
        return obj.curators.count()

    @admin.display(description="# sources")
    def source_count(self, obj):
        return obj.sources.count()

    @admin.display(description="OA works (total)")
    def openalex_total_works_display(self, obj):
        total = 0
        has_any = False
        for src in obj.sources.all():
            count = (src.statistics or {}).get("openalex_works_count")
            if count is not None:
                total += count
                has_any = True
        return total if has_any else "—"

    @admin.action(description="Publish selected collections")
    def publish_collections(self, request, queryset):
        n = queryset.update(is_published=True)
        self.message_user(request, f"Published {n} collection(s).", level=messages.SUCCESS)

    @admin.action(description="Unpublish selected collections")
    def unpublish_collections(self, request, queryset):
        n = queryset.update(is_published=False)
        self.message_user(request, f"Unpublished {n} collection(s).", level=messages.SUCCESS)


@admin.register(ServiceToken)
class ServiceTokenAdmin(admin.ModelAdmin):
    """Stored API credentials for external-service connectors (e.g. OpenAIRE).

    Lets staff rotate a refresh token without SSH: paste the new value into the
    "Refresh token" field and save. The "Refresh access token now" action
    immediately exchanges it so you can verify it works.
    """

    list_display = (
        "service",
        "has_refresh_token",
        "refresh_token_set_at",
        "expires_in_display",
        "access_token_status",
    )
    fields = (
        "service",
        "refresh_token",
        "refresh_token_set_at",
        "expires_in_display",
        "access_token_status",
        "access_token_expires_at",
        "last_reminder_sent_at",
        "updated_at",
    )
    readonly_fields = (
        "refresh_token_set_at",
        "expires_in_display",
        "access_token_status",
        "access_token_expires_at",
        "last_reminder_sent_at",
        "updated_at",
    )
    actions = ["refresh_access_token_now"]

    @admin.display(description="Refresh token set", boolean=True)
    def has_refresh_token(self, obj):
        return bool(obj.refresh_token)

    @admin.display(description="Refresh token expires")
    def expires_in_display(self, obj):
        expires = obj.refresh_token_expires_at
        if expires is None:
            return "—"
        days = obj.days_until_refresh_expiry()
        return f"{expires:%Y-%m-%d} ({days} day(s) left)"

    @admin.display(description="Access token")
    def access_token_status(self, obj):
        if obj.access_token_valid():
            return format_html(
                '<span style="color:green">valid until {}</span>',
                f"{obj.access_token_expires_at:%Y-%m-%d %H:%M}",
            )
        if obj.access_token:
            return format_html('<span style="color:#a00">expired</span>')
        return "—"

    def save_model(self, request, obj, form, change):
        """Route refresh-token edits through set_refresh_token so timestamps/caches reset."""
        if "refresh_token" in form.changed_data:
            new_value = form.cleaned_data.get("refresh_token", "")
            # Ensure the row exists before set_refresh_token() saves it.
            if obj.pk is None:
                obj.refresh_token = ""
                super().save_model(request, obj, form, change)
            obj.set_refresh_token(new_value)
        else:
            super().save_model(request, obj, form, change)

    @admin.action(description="Refresh access token now")
    def refresh_access_token_now(self, request, queryset):
        from works.harvesting.openaire import get_openaire_access_token

        # Currently only OpenAIRE has an exchange flow; generalise per service as needed.
        exchangers = {ServiceToken.OPENAIRE: get_openaire_access_token}
        for token_row in queryset:
            exchanger = exchangers.get(token_row.service)
            if exchanger is None:
                messages.warning(request, f"No token exchange configured for {token_row.get_service_display()}.")
                continue
            if not token_row.refresh_token:
                messages.error(request, f"{token_row.get_service_display()}: no refresh token set.")
                continue
            try:
                access = exchanger()
            except Exception as exc:  # noqa: BLE001
                messages.error(request, f"{token_row.get_service_display()}: exchange failed — {exc}")
                continue
            if access:
                messages.success(request, f"{token_row.get_service_display()}: obtained a fresh access token.")
            else:
                messages.error(
                    request,
                    f"{token_row.get_service_display()}: exchange returned no token (check the refresh token / logs).",
                )
