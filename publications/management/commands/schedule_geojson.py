from django.core.management.base import BaseCommand
from django_q.tasks import schedule
from django_q.models import Schedule

class Command(BaseCommand):
    help = "Schedule the GeoJSON regeneration task to run every 6 hours."

    def handle(self, *args, **kwargs):
        # Check if a schedule already exists for regenerate_geojson
        if not Schedule.objects.filter(func='publications.tasks.regenerate_geojson').exists():
            # Schedule the task to run every 6 hours (360 minutes)
            schedule(
                'publications.tasks.regenerate_geojson',
                schedule_type='I',   # 'I' stands for interval
                minutes=1,         # SHould be 360 minutes = 6 hours, set at one for testing.
                repeats=-1           # repeat indefinitely
            )
            self.stdout.write(self.style.SUCCESS("Scheduled GeoJSON regeneration every 6 hours."))
        else:
            self.stdout.write(self.style.WARNING("GeoJSON regeneration task is already scheduled."))
