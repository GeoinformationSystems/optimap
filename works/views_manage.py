# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Staff-only harvesting/sources management UI (issues #133, #44) and the
async-task/cluster-monitoring dashboard (issue #130).

Plain Django views (not DRF) — kept out of the public ``/api/v1/`` schema.
URL design follows the "nouns not verbs" convention (see CLAUDE.md): each
resource path serves both the read (GET) and write (POST) side, with no
``…/trigger/`` or ``…/status.json`` verb endpoints. Live progress (harvest
logs, cluster/queue health) is content-negotiated on the same resource via
``?format=json``, polled with a plain ``fetch()`` loop (see docs/manage.md —
"WebSocket library research" for why polling was chosen over Channels/SSE).
"""

import logging

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Count, Max, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django_q.tasks import async_task

from works.models import Country, GlobalRegion, HarvestingEvent, Source, Work
from works.utils.pagination import paginate_works

logger = logging.getLogger(__name__)

MANAGE_PAGE_SIZE = 25
RECORDS_PAGE_SIZE = 50

# Optional Source fields edited as free text on the create/edit forms —
# blank means "unset" (None), not an empty string.
_OPTIONAL_SOURCE_FIELDS = ("openalex_id", "issn_l", "doi_prefix", "homepage_url")


def _clean(post, field):
    """Return ``post[field]`` stripped, or ``None`` when blank."""
    return (post.get(field) or "").strip() or None


def _paginate(request, queryset, page_param="page", page_size=MANAGE_PAGE_SIZE):
    paginator = Paginator(queryset, page_size)
    try:
        page_obj = paginator.page(request.GET.get(page_param, 1))
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    return page_obj


def _trigger_harvest(source, requesting_user, max_records=None, trigger_source="ui"):
    """Pre-create a ``pending`` HarvestingEvent and enqueue the matching harvester.

    Mirrors the ``harvest_sources --async`` pattern (see
    works/management/commands/harvest_sources.py): pre-creating the event lets
    the caller redirect straight to a stable event detail URL before the
    Django-Q task has actually started; the task reuses this row via
    ``start_harvesting_event`` rather than creating its own. No
    ``trigger_source`` kwarg is passed to the task itself — the pre-created
    event already carries the authoritative value, and ``event_id`` makes
    ``start_harvesting_event`` reuse that row without ever consulting a
    ``trigger_source`` argument.

    Returns ``(event_or_none, error_message_or_none)``.
    """
    task_func = Source.SOURCE_TYPE_TASKS.get(source.source_type)
    if not task_func:
        return None, f"No harvester registered for source_type={source.source_type!r}."
    user = requesting_user if requesting_user and requesting_user.is_authenticated else None
    event = HarvestingEvent.objects.create(source=source, status="pending", trigger_source=trigger_source, user=user)
    async_task(task_func, source.id, user.id if user else None, max_records=max_records, event_id=event.id)
    return event, None


# -----------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------


@never_cache
@staff_member_required
def dashboard(request):
    """``GET /manage/`` — counts, running harvests, quick links, cluster health.

    ``?format=json`` returns just the live-refresh widgets (running harvests +
    cluster/queue health) for the dashboard's polling loop.
    """
    from works.qmonitor import cluster_stats, queue_depth, recent_task_failures, upcoming_schedules

    running_events = (
        HarvestingEvent.objects.filter(status="in_progress").select_related("source").order_by("-started_at")[:10]
    )
    recent_events = HarvestingEvent.objects.select_related("source").order_by("-started_at")[:10]

    if request.GET.get("format") == "json":
        failures = recent_task_failures(limit=5)
        for f in failures:
            f["detail_url"] = reverse("optimap:manage-task-detail", args=[f["id"]])
        return JsonResponse(
            {
                "running_harvests": [
                    {
                        "id": e.id,
                        "source": e.source.name,
                        "started_at": e.started_at.isoformat(),
                        "detail_url": reverse("optimap:manage-harvest-detail", args=[e.id]),
                        "is_stale": e.is_stale,
                        "staleness_message": e.staleness_message,
                    }
                    for e in running_events
                ],
                "cluster": cluster_stats(),
                "queue_depth": queue_depth(),
                "recent_failures": failures,
            }
        )

    context = {
        "source_count": Source.objects.count(),
        "work_counts": dict(Work.objects.values_list("status").annotate(n=Count("id"))),
        "running_events": running_events,
        "recent_events": recent_events,
        "cluster": cluster_stats(),
        "queue_depth": queue_depth(),
        "upcoming_schedules": upcoming_schedules(limit=5),
        "recent_failures": recent_task_failures(limit=5),
        # The country/region point-in-polygon join runs automatically on every
        # harvest save (works.signals.assign_work_countries/assign_work_regions),
        # but silently links nothing if these reference tables were never
        # loaded — no error, just a quietly-empty Work.countries/regions. Flag
        # it here rather than let it look like the enrichment itself is broken.
        "country_table_empty": not Country.objects.real().exists(),
        "region_table_empty": not GlobalRegion.objects.exists(),
    }
    return render(request, "manage/dashboard.html", context)


# -----------------------------------------------------------------------------
# Sources
# -----------------------------------------------------------------------------


@never_cache
@staff_member_required
def sources(request):
    """``GET /manage/sources/`` — paginated source list. ``POST`` creates a source
    (step 2 of the discovery flow, or a plain manual add)."""
    if request.method == "POST":
        return _create_source(request)

    qs = Source.objects.annotate(last_event_at=Max("harvesting_events__started_at")).order_by("name")
    source_type = request.GET.get("source_type")
    if source_type:
        qs = qs.filter(source_type=source_type)
    search = request.GET.get("q")
    if search:
        qs = qs.filter(name__icontains=search)

    page_obj = _paginate(request, qs)
    context = {
        "page_obj": page_obj,
        "page_size": MANAGE_PAGE_SIZE,
        "source_types": Source.SOURCE_TYPE_CHOICES,
        "selected_source_type": source_type or "",
        "search": search or "",
    }
    return render(request, "manage/sources_list.html", context)


def _find_duplicate_sources(url_field=None, issn_l=None, openalex_id=None, exclude_id=None):
    """Existing ``Source`` rows that share a URL/ISSN-L/OpenAlex id with the
    given candidate values — a soft duplicate-detection signal for the "Add
    source" form, not a hard uniqueness constraint. A shared DOI prefix
    between *different* sources can be entirely legitimate (e.g. ESSOAr and
    Authorea share ``10.22541``, disambiguated at harvest time via
    ``doi_contains``/ISSN — see ``harvest_sources.py``), so ``doi_prefix`` is
    deliberately not checked here.
    """
    conditions = Q()
    if url_field:
        conditions |= Q(url_field__iexact=url_field.strip())
    if issn_l:
        conditions |= Q(issn_l__iexact=issn_l.strip())
    if openalex_id:
        conditions |= Q(openalex_id__iexact=openalex_id.strip())
    if not conditions:
        return []
    qs = Source.objects.filter(conditions)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    return list(qs)


def _create_source(request):
    name = (request.POST.get("name") or "").strip()
    url_field = (request.POST.get("url_field") or "").strip()
    source_type = request.POST.get("source_type") or "oai-pmh"
    if not name or not url_field:
        messages.error(request, "Name and URL are required to create a source.")
        return redirect("optimap:manage-source-new")

    optional_values = {field: _clean(request.POST, field) for field in _OPTIONAL_SOURCE_FIELDS}
    if request.POST.get("confirm_duplicate") != "1":
        duplicates = _find_duplicate_sources(
            url_field=url_field,
            issn_l=optional_values.get("issn_l"),
            openalex_id=optional_values.get("openalex_id"),
        )
        if duplicates:
            names = ", ".join(f"{s.name!r} (id={s.id})" for s in duplicates)
            messages.warning(
                request,
                f"Not created — this looks like it might already exist as {names}. "
                "Review the existing source, or resubmit to create it anyway.",
            )
            return redirect("optimap:manage-source-new")

    source = Source.objects.create(
        name=name,
        url_field=url_field,
        source_type=source_type,
        **optional_values,
    )
    messages.success(request, f"Created source {source.name!r}.")
    return redirect("optimap:manage-source-detail", source_id=source.id)


@never_cache
@staff_member_required
def source_new(request):
    """``GET /manage/sources/new/`` — create form.

    ``?discover=<url>`` runs OAI-PMH/OJS discovery and shows the candidate
    repositories (with their ``Identify`` metadata) as a read-only preview;
    the admin then picks one and submits the create form (``POST
    /manage/sources/``), prefilled from the chosen candidate.

    ``?format=json`` supports three independent live-lookup params used by
    the create form's JS (never combined in one request):

    - ``openalex_search=<name>`` — up to 5 candidate OpenAlex sources (id,
      ISSN-L, homepage URL, works count) for the "Suggest from OpenAlex"
      button next to the OpenAlex source ID field.
    - ``openalex_doi_prefix=<id>`` — best-effort DOI-prefix guess for a
      chosen OpenAlex source id (see ``openalex_discovery.guess_doi_prefix``;
      deferred until a candidate is actually picked, to avoid one extra
      OpenAlex request per candidate shown).
    - ``check_duplicate=1`` (+ ``url_field``/``issn_l``/``openalex_id``) —
      existing sources that might already be this one (see
      ``_find_duplicate_sources``), for a live inline warning before submit.
    """
    if request.GET.get("format") == "json":
        if "openalex_search" in request.GET:
            from works.harvesting.openalex_discovery import search_openalex_sources

            return JsonResponse({"candidates": search_openalex_sources(request.GET.get("openalex_search"))})
        if "openalex_doi_prefix" in request.GET:
            from works.harvesting.openalex_discovery import guess_doi_prefix

            return JsonResponse({"doi_prefix": guess_doi_prefix(request.GET.get("openalex_doi_prefix"))})
        if request.GET.get("check_duplicate") == "1":
            matches = _find_duplicate_sources(
                url_field=request.GET.get("url_field"),
                issn_l=request.GET.get("issn_l"),
                openalex_id=request.GET.get("openalex_id"),
            )
            return JsonResponse(
                {
                    "matches": [
                        {
                            "id": s.id,
                            "name": s.name,
                            "detail_url": reverse("optimap:manage-source-detail", args=[s.id]),
                        }
                        for s in matches
                    ]
                }
            )

    candidates = []
    discover_url = request.GET.get("discover")
    discover_error = None
    if discover_url:
        from works.harvesting.discovery import discover_oai_endpoint

        try:
            candidates = discover_oai_endpoint(discover_url)
        except Exception as exc:  # noqa: BLE001 — surface any discovery failure to the admin
            discover_error = str(exc)

    context = {
        "source_types": Source.SOURCE_TYPE_CHOICES,
        "discover_url": discover_url or "",
        "candidates": candidates,
        "discover_error": discover_error,
    }
    return render(request, "manage/source_form.html", context)


@never_cache
@staff_member_required
def source_detail(request, source_id):
    """``GET /manage/sources/<id>/`` — config, schedule, recent events, paginated
    Works. ``POST`` applies an edit-form submission (see ``source_edit``)."""
    source = get_object_or_404(Source, pk=source_id)
    if request.method == "POST":
        return _update_source(request, source)

    # Reuses the shared Work-list paginator (works/utils/pagination.py) — this
    # list is Work rows, so it gets the same ?size/?page clamping and
    # page-size picker as every other work listing, instead of a bespoke one.
    works_qs = Work.objects.filter(source=source).order_by("-creationDate", "-id")
    page_obj, page_size = paginate_works(request, works_qs)
    recent_events = source.harvesting_events.order_by("-started_at")[:10]
    context = {
        "source": source,
        "page_obj": page_obj,
        "page_size": page_size,
        "recent_events": recent_events,
        "has_task": bool(Source.SOURCE_TYPE_TASKS.get(source.source_type)),
    }
    return render(request, "manage/source_detail.html", context)


def _update_source(request, source):
    source.name = (request.POST.get("name") or source.name).strip()
    source.url_field = (request.POST.get("url_field") or source.url_field).strip()
    source.source_type = request.POST.get("source_type") or source.source_type
    interval_raw = (request.POST.get("harvest_interval_minutes") or "").strip()
    source.harvest_interval_minutes = int(interval_raw) if interval_raw.isdigit() else 0
    for field in _OPTIONAL_SOURCE_FIELDS:
        setattr(source, field, _clean(request.POST, field))
    source.save()
    messages.success(request, f"Updated source {source.name!r}.")
    return redirect("optimap:manage-source-detail", source_id=source.id)


@never_cache
@staff_member_required
def source_edit(request, source_id):
    """``GET /manage/sources/<id>/edit/`` — edit form; submits to the detail URL."""
    source = get_object_or_404(Source, pk=source_id)
    context = {"source": source, "source_types": Source.SOURCE_TYPE_CHOICES}
    return render(request, "manage/source_form.html", context)


@never_cache
@staff_member_required
@require_POST
def source_delete(request, source_id):
    """``POST /manage/sources/<id>/delete/`` — guarded delete.

    Refuses to delete a source that still has works attached — point the
    admin at the Django admin's "migrate_source_works" tooling instead of
    silently orphaning (``SET_NULL``) hundreds of works.
    """
    source = get_object_or_404(Source, pk=source_id)
    work_count = source.works.count()
    if work_count:
        messages.error(
            request,
            f"Cannot delete {source.name!r} — it still has {work_count} work(s). "
            "Reassign or delete those first (see the Django admin).",
        )
        return redirect("optimap:manage-source-detail", source_id=source.id)
    name = source.name
    source.delete()
    messages.success(request, f"Deleted source {name!r}.")
    return redirect("optimap:manage-sources")


# -----------------------------------------------------------------------------
# Harvests
# -----------------------------------------------------------------------------


@never_cache
@staff_member_required
def harvests(request):
    """``GET /manage/harvests/`` — paginated events, filterable by status/source/
    trigger origin. ``POST`` triggers a harvest for a chosen source."""
    if request.method == "POST":
        return _trigger_harvest_view(request)

    qs = HarvestingEvent.objects.select_related("source").order_by("-started_at")
    status = request.GET.get("status")
    if status:
        qs = qs.filter(status=status)
    source_id = request.GET.get("source")
    if source_id and source_id.isdigit():
        qs = qs.filter(source_id=source_id)
    trigger_source = request.GET.get("trigger_source")
    if trigger_source:
        qs = qs.filter(trigger_source=trigger_source)

    page_obj = _paginate(request, qs)
    context = {
        "page_obj": page_obj,
        "page_size": MANAGE_PAGE_SIZE,
        "statuses": HarvestingEvent._meta.get_field("status").choices,
        "trigger_sources": HarvestingEvent.TRIGGER_SOURCE_CHOICES,
        "sources": Source.objects.order_by("name"),
        "selected_status": status or "",
        "selected_source": source_id or "",
        "selected_trigger_source": trigger_source or "",
    }
    return render(request, "manage/harvests_list.html", context)


def _trigger_harvest_view(request):
    source_id = request.POST.get("source")
    if not source_id or not source_id.isdigit():
        messages.error(request, "Choose a source to harvest.")
        return redirect("optimap:manage-harvests")
    source = get_object_or_404(Source, pk=source_id)
    max_records_raw = (request.POST.get("max_records") or "").strip()
    max_records = int(max_records_raw) if max_records_raw.isdigit() else None
    event, error = _trigger_harvest(source, request.user, max_records=max_records, trigger_source="ui")
    if error:
        messages.error(request, error)
        return redirect("optimap:manage-source-detail", source_id=source.id)
    messages.success(request, f"Harvest queued for {source.name!r}.")
    return redirect("optimap:manage-harvest-detail", event_id=event.id)


@never_cache
@staff_member_required
def harvest_detail(request, event_id):
    """``GET /manage/harvests/<id>/`` — event detail + live log. ``POST``
    cancels a still-queued (``pending``) event.

    ``?format=json&since=<chars-seen>`` returns the polling payload: new log
    text since ``since``, current status, and live counters/lists while the
    event is still running. ``HarvestingEvent.records_added``/etc. are only
    ever written once, at the end of the run (``complete_harvest()``), so
    while a harvest is still ``pending``/``in_progress`` the counts are read
    live from ``HarvestRecord``/``count_spatial_temporal`` instead — otherwise
    the stat boxes would show "—" for the run's whole duration even as the
    newly-harvested list already grows underneath them. ``created_records``/
    ``updated_records`` (title + landing-page URL, first page only) are
    likewise only included while running, so the client can keep those lists
    live too; once terminal, the page does a one-time reload for the final
    server-rendered state instead.
    """
    event = get_object_or_404(HarvestingEvent.objects.select_related("source"), pk=event_id)

    if request.method == "POST":
        return _cancel_harvest_view(request, event)

    if request.GET.get("format") == "json":
        from works.harvesting.common import count_spatial_temporal

        since = request.GET.get("since")
        try:
            since = int(since)
        except (TypeError, ValueError):
            since = 0
        log_text = event.log_text or ""
        is_running = event.status in ("pending", "in_progress")
        if is_running:
            records_added = event.records.filter(action="created").count()
            records_updated = event.records.filter(action="updated").count()
            records_with_spatial, records_with_temporal = count_spatial_temporal(event)
        else:
            records_added = event.records_added
            records_updated = event.records_updated
            records_with_spatial = event.records_with_spatial
            records_with_temporal = event.records_with_temporal
        payload = {
            "status": event.status,
            "log_tail": log_text[since:],
            "log_length": len(log_text),
            "records_added": records_added,
            "records_updated": records_updated,
            "records_skipped": event.records_skipped,
            "records_with_spatial": records_with_spatial,
            "records_with_temporal": records_with_temporal,
            "error_message": event.error_message,
            "completed_at": event.completed_at.isoformat() if event.completed_at else None,
            "is_stale": event.is_stale,
            "staleness_message": event.staleness_message,
        }
        if is_running:

            def _brief(action):
                qs = event.records.filter(action=action).select_related("work").order_by("-created_at")
                return [
                    {
                        "title": r.work.title or "",
                        "url": reverse("optimap:work-landing", args=[r.work.get_identifier()]),
                    }
                    for r in qs[:RECORDS_PAGE_SIZE]
                ]

            payload["created_records"] = _brief("created")
            payload["updated_records"] = _brief("updated")
        return JsonResponse(payload)

    created_page = _paginate(
        request,
        event.records.filter(action="created").select_related("work").order_by("-created_at"),
        page_param="created_page",
        page_size=RECORDS_PAGE_SIZE,
    )
    updated_page = _paginate(
        request,
        event.records.filter(action="updated").select_related("work").order_by("-created_at"),
        page_param="updated_page",
        page_size=RECORDS_PAGE_SIZE,
    )
    context = {
        "event": event,
        "created_page": created_page,
        "updated_page": updated_page,
        "page_size": RECORDS_PAGE_SIZE,
        "is_running": event.status in ("pending", "in_progress"),
        "can_cancel": event.status == "pending",
    }
    return render(request, "manage/harvest_detail.html", context)


def _cancel_harvest_view(request, event):
    """Cancel a still-``pending`` harvest by removing its queued Django-Q task.

    Only a *queued* event can be cancelled this way — once a worker has
    picked up the task there is no safe way to stop it mid-flight (see
    docs/manage.md). If the matching ``OrmQ`` row can't be found (the worker
    grabbed it a moment ago), the event is still marked ``cancelled`` but with
    a warning, since the harvester may still run to completion.
    """
    if event.status != "pending":
        messages.error(request, "Only a still-queued harvest can be cancelled.")
        return redirect("optimap:manage-harvest-detail", event_id=event.id)

    from works.qmonitor import find_queued_task_for_event

    queued_task = find_queued_task_for_event(event.id)
    if queued_task is not None:
        queued_task.delete()

    event.status = "cancelled"
    event.error_message = "Cancelled by staff before it started."
    event.completed_at = timezone.now()
    event.save(update_fields=["status", "error_message", "completed_at"])

    if queued_task is not None:
        messages.success(request, "Harvest cancelled before it started.")
    else:
        messages.warning(
            request,
            "Marked as cancelled, but no matching queued task was found — "
            "it may already have started running and will complete anyway.",
        )
    return redirect("optimap:manage-harvest-detail", event_id=event.id)


# -----------------------------------------------------------------------------
# Async task & cluster monitoring (#130)
# -----------------------------------------------------------------------------


@never_cache
@staff_member_required
def tasks(request):
    """``GET /manage/tasks/`` — paginated Success/Failure task browser, plus
    the still-queued (not yet picked up by a worker) tasks from ``OrmQ``.

    The queued section exists because ``task_queryset()`` alone only shows
    completed history — a task can be genuinely enqueued yet invisible here
    for a while, especially when every configured worker is already busy with
    something else.
    """
    from works.qmonitor import queued_tasks, task_queryset

    status = request.GET.get("status")
    func = request.GET.get("func")
    group = request.GET.get("group")
    qs = task_queryset(status=status, func=func, group=group)
    page_obj = _paginate(request, qs)
    context = {
        "page_obj": page_obj,
        "page_size": MANAGE_PAGE_SIZE,
        "selected_status": status or "",
        "selected_func": func or "",
        "selected_group": group or "",
        "queued_tasks": queued_tasks(),
    }
    return render(request, "manage/tasks_list.html", context)


@never_cache
@staff_member_required
def task_detail(request, task_id):
    """``GET /manage/tasks/<id>/`` — task detail: func, args/kwargs, result/traceback.

    Falls back to the still-queued/running ``OrmQ`` entry (see
    ``qmonitor.get_queued_task``) when there's no ``Task`` row yet — a task
    only gets one once Django-Q's monitor has a result for it, so the
    "Queued" list's links would otherwise 404 for anything not yet finished.
    """
    from works.qmonitor import get_queued_task, get_task

    task = get_task(task_id)
    if task is not None:
        return render(request, "manage/task_detail.html", {"task": task, "queued": None, "task_id": task_id})
    queued = get_queued_task(task_id)
    if queued is None:
        raise Http404("Task not found (results may have expired or the id is invalid).")
    return render(request, "manage/task_detail.html", {"task": None, "queued": queued, "task_id": task_id})
