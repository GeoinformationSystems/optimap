"""Publications API URL Configuration."""

from rest_framework import routers, viewsets
from publications.viewsets import PublicationViewSet, SubscriptionViewset
from .models import Publication
from .serializers import PublicationSerializer


class JournalViewSet(viewsets.ReadOnlyModelViewSet):
    """
    A viewset for viewing journal-specific metadata.
    """
    queryset = Publication.objects.filter(journal_identifier__isnull=False)
    serializer_class = PublicationSerializer


# Set up routers
router = routers.DefaultRouter()
router.register(r"publications", PublicationViewSet)
router.register(r"subscriptions", SubscriptionViewset, basename='subscription')
router.register(r"journals", JournalViewSet, basename='journal')  # Add JournalViewSet

# URL patterns
urlpatterns = router.urls
