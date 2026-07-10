# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read-only helpers over Django-Q's own models/cache, for the ``/manage/``
async-task and cluster-monitoring UI (issue #130).

No new models — Q_CLUSTER uses the ORM broker (``optimap/settings.py``), so
task history lives in ``django_q.models.Task`` (+ the ``Success``/``Failure``
proxies), the pending queue in ``django_q.models.OrmQ``, and schedules in
``django_q.models.Schedule``. Cluster heartbeats (``Stat.get_all()``) are a
separate mechanism: they go through Django's cache framework
(``Broker.get_cache()``, alias ``"default"``), not the ORM broker tables —
easy to conflate, so this module is the one place that reads all three.

Every function degrades gracefully (returns an empty list/zero) when there is
no active cluster or the relevant table is empty, rather than raising — the
dashboard should render fine on a fresh install before ``qcluster`` has ever
run.
"""

import logging

logger = logging.getLogger(__name__)


def cluster_stats() -> list[dict]:
    """Live Django-Q cluster heartbeats (host, pid, uptime, queue/task counts).

    Empty list when no cluster has reported in (``qcluster`` not running, or
    its heartbeat has expired from the cache).
    """
    try:
        from django_q.status import Stat

        stats = Stat.get_all()
    except Exception as exc:  # noqa: BLE001 — a broken cache backend must not break the dashboard
        logger.warning("qmonitor.cluster_stats: could not read cluster stats: %s", exc)
        return []

    return [
        {
            "host": s.host,
            "cluster_id": getattr(s, "cluster_id", None),
            "pid": s.pid,
            "status": s.status,
            "uptime": s.uptime(),
            "workers": len(s.workers) if s.workers else 0,
            "task_q_size": s.task_q_size,
            "done_q_size": s.done_q_size,
        }
        for s in stats
    ]


def queue_depth() -> int:
    """Number of tasks currently waiting in the ORM broker queue (``OrmQ``)."""
    try:
        from django_q.models import OrmQ

        return OrmQ.objects.count()
    except Exception as exc:  # noqa: BLE001
        logger.warning("qmonitor.queue_depth: could not read OrmQ: %s", exc)
        return 0


def queued_tasks(limit=None) -> list[dict]:
    """Tasks currently waiting in the ORM broker queue (``OrmQ``) — enqueued
    but not yet picked up by a worker, so they have no ``Task`` row yet.

    ``/manage/tasks/`` otherwise only shows ``task_queryset()`` (completed
    ``Success``/``Failure`` history), so a task can be genuinely enqueued and
    still be invisible there for a while — most noticeably when every
    configured worker (``Q_CLUSTER["workers"]``) is already busy, so a task
    can sit here for the full duration of whatever's currently running before
    a worker ever touches it.
    """
    try:
        from django.utils import timezone
        from django_q.models import OrmQ

        now = timezone.now()
        qs = OrmQ.objects.all().order_by("id")
        if limit:
            qs = qs[:limit]
        results = []
        for row in qs:
            # OrmQ.lock is not "unset until claimed" — enqueue() stamps it
            # `now()` immediately, and a worker claiming the row pushes it to
            # `now() + Q_CLUSTER["retry"]` (see django_q.brokers.orm). So a
            # lock in the past means "ready, waiting for a worker"; a lock in
            # the future means "a worker has it, until that lease expires".
            picked_up = bool(row.lock and row.lock > now)
            task_id = row.task_id()
            results.append(
                {
                    "id": row.id,
                    "task_id": task_id,
                    "func": row.func(),
                    "name": row.name(),
                    "group": row.group(),
                    "picked_up": picked_up,
                    "locked_until": row.lock if picked_up else None,
                    # A corrupted/unsigned payload falls back to a synthetic
                    # "*ExceptionName" id (see OrmQ.task) — not a real task id,
                    # so there's nothing meaningful to link to.
                    "linkable": bool(task_id) and not str(task_id).startswith("*"),
                }
            )
        return results
    except Exception as exc:  # noqa: BLE001 — a broken cache/DB read must not break the dashboard
        logger.warning("qmonitor.queued_tasks: could not read OrmQ: %s", exc)
        return []


def find_queued_task_for_event(event_id):
    """Return the still-queued ``OrmQ`` row whose task kwargs carry this
    ``HarvestingEvent`` id, or ``None`` if none is found.

    Only harvests pre-created as ``pending`` (the ``/manage/`` UI trigger and
    ``harvest_sources --async``) pass ``event_id`` as a task kwarg, so this is
    also how ``/manage/harvests/<id>/`` distinguishes a genuinely still-queued
    event (cancellable) from one a worker has already picked up.
    """
    from django_q.models import OrmQ

    for row in OrmQ.objects.all():
        kwargs = row.kwargs() or {}
        if kwargs.get("event_id") == event_id:
            return row
    return None


def upcoming_schedules(limit=5) -> list[dict]:
    """The next ``limit`` recurring/one-off Django-Q schedules due to fire.

    ``label`` is always non-empty: many schedules (e.g. the built-in weekly
    sweeps created via ``django_q.tasks.schedule()``) are never given an
    explicit ``name``, so falling back to the dotted ``func`` path avoids a
    blank/"None" entry in the dashboard's "Upcoming schedules" list.
    """
    from django_q.models import Schedule

    return [
        {
            "id": s.id,
            "name": s.name,
            "label": s.name or s.func,
            "func": s.func,
            "schedule_type": s.get_schedule_type_display(),
            "next_run": s.next_run,
        }
        for s in Schedule.objects.exclude(repeats=0).order_by("next_run")[:limit]
    ]


def recent_task_failures(limit=5) -> list[dict]:
    """The ``limit`` most recent failed Django-Q tasks (``Failure`` proxy model)."""
    from django_q.models import Failure

    return [
        {
            "id": t.id,
            "name": t.name,
            "func": t.func,
            "stopped": t.stopped,
            "result": _short_result(t.result),
        }
        for t in Failure.objects.order_by("-stopped")[:limit]
    ]


def _short_result(result, max_length=300) -> str:
    text = str(result) if result is not None else ""
    return (text[:max_length] + "…") if len(text) > max_length else text


def task_queryset(status=None, func=None, group=None):
    """Filtered/ordered ``Task`` queryset for the ``/manage/tasks/`` browser.

    ``status`` is ``"success"`` / ``"failed"`` / ``None`` (both). ``func`` and
    ``group`` are case-insensitive substring filters.
    """
    from django_q.models import Task

    qs = Task.objects.all().order_by("-stopped")
    if status == "success":
        qs = qs.filter(success=True)
    elif status == "failed":
        qs = qs.filter(success=False)
    if func:
        qs = qs.filter(func__icontains=func)
    if group:
        qs = qs.filter(group__icontains=group)
    return qs


def get_task(task_id):
    """Return the ``Task`` with this id, or ``None`` (for ``/manage/tasks/<id>/``)."""
    from django_q.models import Task

    return Task.objects.filter(id=task_id).first()


def get_queued_task(task_id):
    """Return the still-queued/running ``OrmQ`` entry for this task id (same
    shape as one row from :func:`queued_tasks`), or ``None``.

    A task only gets a ``Task`` row once Django-Q's monitor has a result for
    it (success or failure) — so a task that's merely waiting or already
    picked up by a worker has no ``Task`` row yet, and ``get_task()`` alone
    would 404 for it. ``/manage/tasks/<id>/`` falls back to this so the
    "Queued" list's links resolve to something.
    """
    for row in queued_tasks():
        if row["task_id"] == task_id:
            return row
    return None
