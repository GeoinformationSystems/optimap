from django import forms
from .models import Subscription
from rest_framework import serializers


class SubscriptionSerializer(serializers.ModelSerializer):
    # Define a serializer field that sources its value from user.username
    user_name = serializers.CharField(source='user.username', read_only=True)

class SubscriptionForm(forms.ModelForm):
    class Meta:
        model = Subscription
        fields = ['name', 'search_term', 'timeperiod_startdate', 'timeperiod_enddate', 'search_area']
