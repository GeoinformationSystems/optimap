"""Publications API URL Configuration."""

from rest_framework import routers
from publications.viewsets import ( SourceViewSet,
    PublicationViewSet,
    SubscriptionViewSet,
)

router = routers.DefaultRouter()
router.register(r"sources", SourceViewSet, basename="source")
router.register(r"publications", PublicationViewSet, basename="publication")
router.register(r"subscriptions", SubscriptionViewSet, basename="subscription")

urlpatterns = router.urls
