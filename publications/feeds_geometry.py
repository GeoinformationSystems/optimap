
import logging
import urllib.parse
from django.http import JsonResponse
from django.contrib.syndication.views import Feed
from .feeds import GeoFeed    
from .models import GlobalRegion, Publication
from django.conf import settings
logger = logging.getLogger(__name__)

class GeoFeedByGeometry(GeoFeed):
    def __init__(self, feed_type_variant="georss"):
        super().__init__(feed_type_variant=feed_type_variant)

    def get_object(self, request, geometry_slug):
        decoded = urllib.parse.unquote(geometry_slug).strip().lower()
        if decoded.endswith(".geojson"):
            decoded = decoded[:-len(".geojson")]
        decoded = decoded.replace("_", " ").replace("-", " ")

        try:
            region = GlobalRegion.objects.get(name=decoded)
            return region

        except GlobalRegion.DoesNotExist:
            logger.warning("Region not found (no name match): %r", decoded)

    def items(self, region):
        return (
            Publication.objects.filter(
                status="p",
                geometry__isnull=False,
                geometry__intersects=region.geom,
            )
            .order_by("-creationDate")[:10]
        )

    def item_link(self, item):
        if item.url:
            return item.url
        else:
            return ""  
