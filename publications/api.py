"""Publications API URL Configuration."""

from rest_framework import routers
from publications.viewsets import ( JournalViewSet,
    PublicationViewSet,
    SubscriptionViewSet,
)

router = routers.DefaultRouter()
router.register(r"journals", JournalViewSet, basename="journal")
router.register(r"publications", PublicationViewSet, basename="publication")
router.register(r"subscriptions", SubscriptionViewSet, basename="subscription")

urlpatterns = router.urls
