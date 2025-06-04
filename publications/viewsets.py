"""publications API views."""
from rest_framework import viewsets
from rest_framework_gis import filters
from .models import Publication, Journal, Subscription
from .serializers import PublicationSerializer, JournalSerializer, SubscriptionSerializer
from rest_framework.permissions import IsAuthenticatedOrReadOnly

class JournalViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Journal.objects.all()
    serializer_class = JournalSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

class PublicationViewSet(viewsets.ReadOnlyModelViewSet):
    bbox_filter_field = "geometry"
    filter_backends = (filters.InBBoxFilter,)
    queryset = Publication.objects.filter(status="p")
    serializer_class = PublicationSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        journal_id = self.request.query_params.get("journal_id")
        if journal_id:
            qs = qs.filter(source__id=journal_id)
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
        user = self.request.user
        return Subscription.objects.filter(user=user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
