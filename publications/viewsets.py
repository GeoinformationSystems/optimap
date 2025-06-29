"""publications API views."""

from rest_framework import viewsets
from rest_framework_gis import filters
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from .models import Publication, Source, Subscription
from .serializers import (
    PublicationSerializer,
    SourceSerializer,
    SubscriptionSerializer,
)

class SourceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Source.objects.all()
    serializer_class = SourceSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

class PublicationViewSet(viewsets.ReadOnlyModelViewSet):
    bbox_filter_field = "geometry"
    filter_backends = (filters.InBBoxFilter,)
    serializer_class = PublicationSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    queryset = Publication.objects.filter(status="p").distinct()

class SubscriptionViewSet(viewsets.ModelViewSet):
    """
    Subscription view set.
    Each user can list, create, update, or delete their own Subscriptions.
    """
    bbox_filter_field = "region"
    filter_backends = (filters.InBBoxFilter,)
    serializer_class = SubscriptionSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        return Subscription.objects.filter(user=user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)