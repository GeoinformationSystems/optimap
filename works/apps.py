# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import logging

from django.apps import AppConfig
from django.conf import settings
from django.db.models.signals import post_migrate
from django.utils import timezone

logger = logging.getLogger(__name__)


def schedule_data_dump(sender, **kwargs):
    from django_q.models import Schedule
    from django_q.tasks import schedule

    func_name = "works.tasks.regenerate_all_data_dumps"
    # Drop legacy single-format schedules so existing deployments migrate to
    # the umbrella that produces GeoJSON + GeoPackage + CSV in one pass.
    legacy = ("works.tasks.regenerate_geopackage_cache", "works.tasks.regenerate_geojson_cache")
    Schedule.objects.filter(func__in=legacy).delete()

    if not Schedule.objects.filter(func=func_name).exists():
        schedule(
            func_name,
            schedule_type="I",
            minutes=settings.DATA_DUMP_INTERVAL_HOURS * 60,
            next_run=timezone.now(),
            repeats=-1,
            intended_date_kwarg="scheduled_for",
        )
        logger.info(
            "Scheduled data‐dump task '%s' every %d hours",
            func_name,
            settings.DATA_DUMP_INTERVAL_HOURS,
        )


def ensure_deleted_user_sentinel(sender, **kwargs):
    from works.models import CustomUser

    CustomUser.objects.get_or_create(
        username="deleted",
        defaults={"email": "", "is_active": False, "is_staff": False},
    )


def schedule_inactivity_tasks(sender, **kwargs):
    from works.tasks import schedule_inactivity_deletion_task, schedule_inactivity_warning_task

    schedule_inactivity_warning_task()
    schedule_inactivity_deletion_task()


def schedule_service_token_tasks(sender, **kwargs):
    from works.tasks import schedule_service_token_renewal_check

    schedule_service_token_renewal_check()


def schedule_country_backfill_tasks(sender, **kwargs):
    from works.tasks import schedule_backfill_work_countries

    schedule_backfill_work_countries()


def schedule_region_backfill_tasks(sender, **kwargs):
    from works.tasks import schedule_backfill_work_regions

    schedule_backfill_work_regions()


def schedule_author_orcid_backfill_tasks(sender, **kwargs):
    from works.tasks import schedule_backfill_author_orcids

    schedule_backfill_author_orcids()


def _update_pygeoapi_extent(sender=None, **kwargs):
    """Compute the bounding box of all published works and patch PYGEOAPI_CONFIG.

    Connected to request_started so it fires after app init (avoids the
    "DB access during app initialization" warning) and disconnects itself
    so it only runs once per process lifetime.
    """
    from django.core.signals import request_started

    request_started.disconnect(_update_pygeoapi_extent)

    if not getattr(settings, "PYGEOAPI_ENABLED", False):
        return
    try:
        from django.contrib.gis.db.models import Extent

        from works.models import Work

        result = Work.objects.filter(status="p").exclude(geometry__isnull=True).aggregate(extent=Extent("geometry"))
        bbox = result.get("extent")
        if bbox:
            settings.PYGEOAPI_CONFIG["resources"]["works"]["extents"]["spatial"]["bbox"] = list(bbox)
            logger.info("Updated pygeoapi works spatial extent to actual data: %s", bbox)
    except Exception as exc:
        logger.debug("Could not update pygeoapi extent at startup (non-fatal): %s", exc)


class WorksConfig(AppConfig):
    name = "works"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from django.core.signals import request_started

        request_started.connect(_update_pygeoapi_extent, weak=False)
        post_migrate.connect(
            schedule_data_dump,
            sender=self,
            weak=False,
            dispatch_uid="works.schedule_data_dump",
        )
        post_migrate.connect(
            ensure_deleted_user_sentinel,
            sender=self,
            weak=False,
            dispatch_uid="works.ensure_deleted_user_sentinel",
        )
        post_migrate.connect(
            schedule_inactivity_tasks,
            sender=self,
            weak=False,
            dispatch_uid="works.schedule_inactivity_tasks",
        )
        post_migrate.connect(
            schedule_service_token_tasks,
            sender=self,
            weak=False,
            dispatch_uid="works.schedule_service_token_tasks",
        )
        post_migrate.connect(
            schedule_country_backfill_tasks,
            sender=self,
            weak=False,
            dispatch_uid="works.schedule_country_backfill_tasks",
        )
        post_migrate.connect(
            schedule_region_backfill_tasks,
            sender=self,
            weak=False,
            dispatch_uid="works.schedule_region_backfill_tasks",
        )
        post_migrate.connect(
            schedule_author_orcid_backfill_tasks,
            sender=self,
            weak=False,
            dispatch_uid="works.schedule_author_orcid_backfill_tasks",
        )
        import works.signals  # noqa: F401 — connects @receiver decorators
