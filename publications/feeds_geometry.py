
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
            region = GlobalRegion.objects.get(name__iexact=decoded)
            return region

        except GlobalRegion.DoesNotExist:
            logger.warning("GeoFeedByGeometry: no GlobalRegion match for %r at URL %s",
                           decoded, request.build_absolute_uri())
            return None

    def items(self, region):
        if region is None:
            logger.warning(
                "GeoFeedByGeometry.items was called with None region")
            return []

        prepared_geom = region.geom.prepared
        candidates = Publication.objects.filter(
            status="p",
            geometry__isnull=False,
            geometry__bboverlaps=region.geom,
        ).order_by("-creationDate")

        return [pub for pub in candidates if prepared_geom.intersects(pub.geometry)][:10]

    def item_link(self, item):
        if item.url:
            return item.url
        else:
            return ""
