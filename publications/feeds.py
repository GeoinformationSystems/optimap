from django.contrib.syndication.views import Feed
from django.utils.feedgenerator import Rss201rev2Feed, Atom1Feed
from .models import Publication
from datetime import datetime
from django.contrib.gis.geos import GEOSGeometry

class CustomGeoFeed(Rss201rev2Feed):
    def __init__(self, *args, **kwargs):
        self.feed_type_variant = kwargs.pop("feed_type_variant", "georss")
        super().__init__(*args, **kwargs)

    def add_root_elements(self, handler):
        super().add_root_elements(handler)
        handler.startPrefixMapping('georss', 'http://www.georss.org/georss')
        handler.startPrefixMapping('geo', 'http://www.w3.org/2003/01/geo/wgs84_pos#')

    def rss_attributes(self):
        return {"version": self._version, "xmlns:atom": "http://www.w3.org/2005/Atom", "xmlns:georss": "http://www.georss.org/georss"}

    def add_item_elements(self, handler, item):
        super().add_item_elements(handler, item)

        if self.feed_type_variant in ["georss", "geoatom"]:
            if "georss_point" in item:
                handler.addQuickElement("georss:point", item["georss_point"])
            if "georss_polygon" in item:
                handler.addQuickElement("georss:polygon", item["georss_polygon"])
            if "georss_line" in item:
                handler.addQuickElement("georss:line", item["georss_line"])

        if self.feed_type_variant in ["w3cgeo", "geoatom"]:
            if "geo_lat" in item and "geo_long" in item:
                handler.addQuickElement("geo:lat", item["geo_lat"])
                handler.addQuickElement("geo:long", item["geo_long"])


class CustomGeoAtomFeed(Atom1Feed):
    def root_attributes(self):
        attrs = super().root_attributes()
        attrs['xmlns:georss'] = 'http://www.georss.org/georss'
        attrs['xmlns:geo'] = 'http://www.w3.org/2003/01/geo/wgs84_pos#'
        return attrs

    def add_root_elements(self, handler):
        super().add_root_elements(handler)
        handler.startPrefixMapping('georss', 'http://www.georss.org/georss')
        handler.startPrefixMapping('geo', 'http://www.w3.org/2003/01/geo/wgs84_pos#')

    def add_item_elements(self, handler, item):
        super().add_item_elements(handler, item)

        if "georss_point" in item:
            handler.addQuickElement("georss:point", item["georss_point"])
        if "georss_polygon" in item:
            handler.addQuickElement("georss:polygon", item["georss_polygon"])
        if "georss_line" in item:
            handler.addQuickElement("georss:line", item["georss_line"])
        if "geo_lat" in item and "geo_long" in item:
            handler.addQuickElement("geo:lat", item["geo_lat"])
            handler.addQuickElement("geo:long", item["geo_long"])

def _format_georss_geometry(geometry):
    georss_data = []

    if geometry.geom_type == "Point":
        lat, lon = geometry.y, geometry.x
        georss_data.append(("georss_point", f"{lat} {lon}"))
        georss_data.append(("geo_lat", str(lat)))  
        georss_data.append(("geo_long", str(lon))) 

    elif geometry.geom_type == "LineString":
        coords = " ".join(f"{pt[1]} {pt[0]}" for pt in geometry.coords)
        georss_data.append(("georss_line", coords))

    elif geometry.geom_type == "Polygon":
        coords = " ".join(f"{pt[1]} {pt[0]}" for pt in geometry.coords[0])
        georss_data.append(("georss_polygon", coords))
        
        centroid = geometry.centroid
        lat, lon = centroid.y, centroid.x
        georss_data.append(("geo_lat", str(lat)))  
        georss_data.append(("geo_long", str(lon))) 

    elif geometry.geom_type == "GeometryCollection":
        for geom in geometry:
            georss_data.extend(_format_georss_geometry(geom))

    return georss_data

class GeoFeed(Feed):
    def __init__(self, feed_type_variant="georss"):
        self.feed_type_variant = feed_type_variant
        super().__init__()

    def get_feed(self, obj, request):
        if self.feed_type_variant == "geoatom":
            self.feed_type = CustomGeoAtomFeed
        else:
            self.feed_type = lambda *args, **kwargs: CustomGeoFeed(*args, **kwargs, feed_type_variant=self.feed_type_variant)
        return super().get_feed(obj, request)

    def title(self):
        return f"Latest Publications ({self.feed_type_variant.upper()})"

    def link(self):
        return f"/feeds/{self.feed_type_variant}/"

    def description(self):
        return f"Updates on the latest publications with geographic data using {self.feed_type_variant.upper()} format."

    def items(self):
        return Publication.objects.filter(status="p", geometry__isnull=False, url__isnull=False).order_by('-creationDate')[:10]

    def item_title(self, item):
        return item.title or "Untitled Publication"

    def item_description(self, item):
        return item.abstract or "No abstract available."

    def item_link(self, item):
        return item.url 

    def item_pubdate(self, item):
        return datetime.combine(item.publicationDate, datetime.min.time()) if item.publicationDate else item.creationDate

    def item_extra_kwargs(self, item):
        georss_elements = {}

        if item.geometry:
            geometries = _format_georss_geometry(item.geometry)

            for key, value in geometries:
                if self.feed_type_variant == "w3cgeo":
                    if key in ["geo_lat", "geo_long"]:
                        georss_elements[key] = value
                else:
                    if key in georss_elements:
                        georss_elements[key] += f" {value}" 
                    else:
                        georss_elements[key] = value  

        return georss_elements