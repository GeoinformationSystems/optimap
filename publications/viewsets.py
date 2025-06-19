"""publications API views."""
from rest_framework import viewsets
from rest_framework_gis import filters
from .models import Publication, Source, Subscription
from .serializers import PublicationSerializer, SourceSerializer, SubscriptionSerializer
from rest_framework.permissions import IsAuthenticatedOrReadOnly

class SourceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Source.objects.all()
    serializer_class = SourceSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

class PublicationViewSet(viewsets.ReadOnlyModelViewSet):
    bbox_filter_field = "geometry"
    filter_backends = (filters.InBBoxFilter,)
    queryset = Publication.objects.filter(status="p")
    serializer_class = PublicationSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        source_id = self.request.query_params.get("source_id")
        if source_id:
            qs = qs.filter(source__id=source_id)
        return qs

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
        return Subscription.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)