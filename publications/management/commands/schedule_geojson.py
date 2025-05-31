from django.core.management.base import BaseCommand
from django_q.tasks    import schedule
from django_q.models   import Schedule

class Command(BaseCommand):
    help = "Schedule the GeoJSON regeneration task every 6 hours."

    def handle(self, *args, **options):
        func_name = 'publications.tasks.regenerate_geojson_cache'
        if not Schedule.objects.filter(func=func_name).exists():
            schedule(
                func_name,
                schedule_type='I',   # interval
                minutes=360,         # every 6 hours
                repeats=-1
            )
            self.stdout.write(self.style.SUCCESS("Scheduled GeoJSON regeneration every 6h."))
        else:
            self.stdout.write("GeoJSON regeneration already scheduled.")