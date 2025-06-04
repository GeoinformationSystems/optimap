"""publications serializers."""

from rest_framework_gis import serializers
from rest_framework import serializers as drf_serializers
from .models import Publication, Subscription, Journal
from django.contrib.auth import get_user_model

User = get_user_model()

class JournalSerializer(drf_serializers.ModelSerializer):
    """Serializer for Journal model."""

    class Meta:
        model = Journal
        fields = [
            "id",
            "name",
            "issn_l",
            "openalex_id",
            "openalex_url",
            "publisher_name",
            "works_count",
            "works_api_url",
        ]

class PublicationSerializer(serializers.GeoFeatureModelSerializer):
    """publication GeoJSON serializer."""
    source_details = JournalSerializer(source="source", read_only=True)

    class Meta:
        model = Publication
        fields = (
            "id",
            "title",
            "abstract",
            "publicationDate",
            "url",
            "doi",
            "creationDate",
            "lastUpdate",
            "timeperiod_startdate",
            "timeperiod_enddate",
            "source",
            "source_details",       
            "geometry",
            "provenance",
        )
        geo_field = "geometry"
        auto_bbox = True      
       
class SubscriptionSerializer(serializers.GeoFeatureModelSerializer):
    """Subscription GeoJSON serializer."""

    class Meta:
        model = Subscription
        fields = ("search_term","timeperiod_startdate","timeperiod_enddate","user")
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
            raise drf_serializers.ValidationError("This email is already registered.")
        return value


class UserSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"] 
