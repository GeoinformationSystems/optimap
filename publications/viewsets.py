"""publications API views."""
from rest_framework import viewsets
from rest_framework_gis import filters
from publications.models import Publication,Subscription
from publications.serializers import PublicationSerializer,SubscriptionSerializer
import requests

class PublicationViewSet(viewsets.ReadOnlyModelViewSet):
    """publication view set."""

    bbox_filter_field = "location"
    filter_backends = (filters.InBBoxFilter,)
    queryset = Publication.objects.all().filter(status="p")
    serializer_class = PublicationSerializer

class SubscriptionViewset(viewsets.ModelViewSet):

    bbox_filter_field = "location"
    filter_backends = (filters.InBBoxFilter,)
    serializer_class = SubscriptionSerializer

    def get_queryset(self):
        user = self.request.user
        queryset = Subscription.objects.filter(user=user)
        return queryset