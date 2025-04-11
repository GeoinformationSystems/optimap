import os
import unittest
from django.test import Client
from django.contrib.auth import get_user_model
User = get_user_model()
from datetime import datetime

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'optimap.settings')

class SimpleTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()

    def test_login(self):
        """Test that fields for logged in users are set correctly"""
        self.user = User.objects.create_user(username="test@example.com", email="test@example.com", password="password")
        self.client.login(username="testuser", password="password")

        # fetch user from DB
        user = User.objects.filter(id=self.user.id).first()

        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.deleted)

        # check the default fields which we do not want to use are emptly
        self.assertEqual(user.first_name, "", "first_name of user must not be set")
        self.assertEqual(user.last_name, "", "last_name of user must not be set")

        timediff_joined = datetime.now(user.date_joined.timetz().tzinfo) - user.date_joined
        self.assertLess(timediff_joined.total_seconds(), 10)

        self.assertEqual(user.username, user.email, "Email and username must be the same")

    @unittest.skip('UI tests need to adjusted for new UI')
    def test_login_page(self):
        response = self.client.get('/login/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get('Content-Type'), 'text/html; charset=utf-8')

        response = self.client.post('/login/', {'email': 'optimap@dev.dev'})
        self.assertEqual(response.status_code, 302)
        self.assertRegex(response.url, 'success')

        # FIXME test login above does not trigger setting the last_login field
        user = User.objects.filter(id=self.user.id).first()
        timediff_login = datetime.now(user.last_login.timetz().tzinfo) - user.last_login
        self.assertLess(timediff_login.total_seconds(), 10)

        # see also https://github.com/GeoinformationSystems/optimap/issues/125

    @unittest.skip('UI tests need to adjusted for new UI')
    def test_login_page_errors(self):
        response = self.client.put('/login/')
        self.assertEqual(response.status_code, 400)
