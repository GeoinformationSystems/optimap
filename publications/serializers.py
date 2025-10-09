"""publications serializers."""

from rest_framework import serializers
from rest_framework_gis.serializers import GeoFeatureModelSerializer
from rest_framework import serializers as drf_serializers
from .models import Publication, Subscription, Source
from django.contrib.auth import get_user_model
from django.conf import settings

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


# Geoextent API Serializers

# Shared field definitions
RESPONSE_FORMAT_CHOICES = ['geojson', 'wkt', 'wkb']
RESPONSE_FORMAT_DEFAULT = 'geojson'
RESPONSE_FORMAT_HELP = "Response format: 'geojson' (default - GeoJSON FeatureCollection), 'wkt' (WKT string with metadata), 'wkb' (WKB hex string with metadata)"

GAZETTEER_CHOICES = ['nominatim', 'geonames', 'photon']
GAZETTEER_DEFAULT = 'nominatim'


class GeoextentBaseSerializer(serializers.Serializer):
    """Base serializer with common geoextent parameters."""
    bbox = serializers.BooleanField(default=True)
    tbox = serializers.BooleanField(default=True)
    convex_hull = serializers.BooleanField(default=False)
    response_format = serializers.ChoiceField(
        choices=RESPONSE_FORMAT_CHOICES,
        default=RESPONSE_FORMAT_DEFAULT,
        help_text=RESPONSE_FORMAT_HELP
    )
    placename = serializers.BooleanField(default=False)
    gazetteer = serializers.ChoiceField(
        choices=GAZETTEER_CHOICES,
        default=GAZETTEER_DEFAULT
    )
    external_metadata = serializers.BooleanField(
        default=True,
        help_text="Retrieve external metadata from CrossRef/DataCite for DOIs (only applies to remote resources)"
    )
    external_metadata_method = serializers.ChoiceField(
        choices=['auto', 'all', 'crossref', 'datacite'],
        default='auto',
        help_text="Method for retrieving metadata: 'auto' (default), 'all', 'crossref', or 'datacite'"
    )

    def validate_gazetteer(self, value):
        """Only validate gazetteer if placename is requested."""
        if self.initial_data.get('placename', False) and not value:
            raise serializers.ValidationError("Gazetteer must be specified when placename=true")
        return value


class GeoextentExtractSerializer(GeoextentBaseSerializer):
    """Serializer for extracting extent from uploaded file."""
    file = serializers.FileField(required=True)


class GeoextentRemoteSerializer(GeoextentBaseSerializer):
    """Serializer for extracting extent from remote repository."""
    identifiers = serializers.ListField(
        child=serializers.CharField(),
        required=True,
        min_length=1,
        help_text="List of DOIs or repository URLs"
    )
    file_limit = serializers.IntegerField(default=10, min_value=1, max_value=100)
    size_limit_mb = serializers.IntegerField(default=100, min_value=1)

    def validate_size_limit_mb(self, value):
        """Ensure requested size doesn't exceed server maximum."""
        max_allowed = getattr(settings, 'GEOEXTENT_MAX_DOWNLOAD_SIZE_MB', 1000)
        if value > max_allowed:
            raise serializers.ValidationError(
                f"Requested size limit ({value}MB) exceeds server maximum ({max_allowed}MB)"
            )
        return value


class GeoextentRemoteGetSerializer(GeoextentBaseSerializer):
    """Serializer for GET endpoint with URL parameters."""
    identifiers = serializers.CharField(
        required=True,
        help_text="Comma-separated DOIs or repository URLs"
    )
    file_limit = serializers.IntegerField(default=10, min_value=1, max_value=100)
    size_limit_mb = serializers.IntegerField(default=100, min_value=1)

    def validate_identifiers(self, value):
        """Parse comma-separated identifiers and validate."""
        identifiers = [i.strip() for i in value.split(',') if i.strip()]
        if not identifiers:
            raise serializers.ValidationError("At least one identifier must be provided")
        return identifiers

    def validate_size_limit_mb(self, value):
        """Ensure requested size doesn't exceed server maximum."""
        max_allowed = getattr(settings, 'GEOEXTENT_MAX_DOWNLOAD_SIZE_MB', 1000)
        if value > max_allowed:
            raise serializers.ValidationError(
                f"Requested size limit ({value}MB) exceeds server maximum ({max_allowed}MB)"
            )
        return value


class GeoextentBatchSerializer(GeoextentBaseSerializer):
    """Serializer for extracting extent from multiple files."""
    # files handled separately in view
    size_limit_mb = serializers.IntegerField(default=100, min_value=1)

    def validate_size_limit_mb(self, value):
        """Ensure total batch size doesn't exceed server maximum."""
        max_allowed = getattr(settings, 'GEOEXTENT_MAX_BATCH_SIZE_MB', 500)
        if value > max_allowed:
            raise serializers.ValidationError(
                f"Requested batch size ({value}MB) exceeds server maximum ({max_allowed}MB)"
            )
        return value
