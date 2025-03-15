"""publications serializers."""

from rest_framework_gis import serializers
from .models import Publication
from django.contrib.auth import get_user_model
User = get_user_model()

from publications.models import Publication,Subscription

class PublicationSerializer(serializers.GeoFeatureModelSerializer):
    """publication GeoJSON serializer."""

    class Meta:
        """publication serializer meta class."""
        model = Publication
        fields = ("id", "title" ,"abstract", "publicationDate", "url", "doi", "creationDate", "lastUpdate", "timeperiod_startdate", "timeperiod_enddate")
        geo_field = "geometry"
        auto_bbox = True      
       
class SubscriptionSerializer(serializers.GeoFeatureModelSerializer):
    """Subscription GeoJSON serializer."""

    class Meta:
        model = Subscription
        fields = ("search_term","timeperiod_startdate","timeperiod_enddate","user_name")
        geo_field = "search_area"
        auto_bbox = True
        
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"] 

    def to_representation(self, instance):
        """Ensure deleted users are excluded from serialization."""
        if instance.deleted:  
            return None 
        return super().to_representation(instance)
