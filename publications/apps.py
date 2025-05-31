import logging
from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

def schedule_data_dump(sender, **kwargs):
    from django_q.models import Schedule
    from django_q.tasks  import schedule

    func_name = "publications.tasks.regenerate_geopackage_cache"
    if not Schedule.objects.filter(func=func_name).exists():
        schedule(
            func_name,
            schedule_type="I",
            minutes=settings.DATA_DUMP_INTERVAL_HOURS * 60,
            next_run=timezone.now(),
            repeats=-1,
        )
        logger.info(
            "Scheduled data‐dump task '%s' every %d hours",
            func_name,
            settings.DATA_DUMP_INTERVAL_HOURS,
        )

class PublicationsConfig(AppConfig):
    name               = "publications"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        import publications.signals  
        post_migrate.connect(
            schedule_data_dump,
            sender=self,
            weak=False,
            dispatch_uid="publications.schedule_data_dump",
        )
