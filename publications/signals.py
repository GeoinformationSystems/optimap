import logging
logger = logging.getLogger(__name__)

from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
User = get_user_model()
from django.conf import settings

@receiver(pre_save, sender=User)
def update_user_callback(sender, instance, **kwargs):
    logging.info('New user added: ', instance.email)

    if instance.email in settings.OPTIMAP_SUPERUSER_EMAILS and not instance.is_superuser:
        logging.warning('Registering user %s as admin', instance.email)
        instance.is_staff = True
        instance.is_superuser = True
