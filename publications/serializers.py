"""publications serializers."""

from rest_framework import serializers
from rest_framework_gis.serializers import GeoFeatureModelSerializer
from rest_framework import serializers as drf_serializers
from .models import Publication, Subscription, Source
from django.contrib.auth import get_user_model

User = get_user_model()

class SourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Source
        fields = (
            "id",
            "name",
            "issn_l",
            "openalex_id",
            "openalex_url",
            "publisher_name",
            "works_count",
            "works_api_url",
        )



class PublicationSerializer(GeoFeatureModelSerializer):
    source_details = serializers.SerializerMethodField()

    class Meta:
        model = Publication
        geo_field = "geometry"
        auto_bbox = True
        fields = [
            "id",
            "title",
            "abstract",
            "publicationDate",
            "doi",
            "url",
            "timeperiod_startdate",
            "timeperiod_enddate",
            "source_details",
        ]

    def get_source_details(self, obj):
        source = obj.source
        if not source:
            return {}
        return SourceSerializer(source, context=self.context).data

class SubscriptionSerializer(GeoFeatureModelSerializer):
    class Meta:
        model = Subscription
        fields = (
            "id",
            "user",
            "name",
            "search_term",
            "timeperiod_startdate",
            "timeperiod_enddate",
            "region",
            "subscribed",
        )

        geo_field = "region"
        auto_bbox = True


class EmailChangeSerializer(serializers.ModelSerializer):
    """Handles email change requests."""

    class Meta:
        model = User
        fields = ["email"]

    def validate_email(self, value):
        """Ensure the new email is not already in use."""
        if User.objects.filter(email=value).exists():
            raise drf_serializers.ValidationError("This email is already registered.")
        return value


class UserSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"]
