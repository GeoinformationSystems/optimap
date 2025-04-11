"""publications serializers."""

from rest_framework_gis import serializers
from .models import Publication
from django.contrib.auth import get_user_model
User = get_user_model()

from publications.models import Publication,Subscription
from django.contrib.auth import get_user_model
User = get_user_model()

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
        geo_field = "region"
        auto_bbox = True
        
class EmailChangeSerializer(serializers.ModelSerializer):  
    """Handles email change requests."""

    class Meta:
        model = User
        fields = ['email']

    def validate_email(self, value):
        """Ensure the new email is not already in use."""
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("This email is already registered.")
        return value

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"] 

    def to_representation(self, instance):
        """Ensure deleted users are excluded from serialization."""
        if instance.deleted:  
            return None 
        return super().to_representation(instance)
