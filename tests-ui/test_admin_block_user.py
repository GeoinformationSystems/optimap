import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

import unittest
from django.test import TransactionTestCase
from helium import *
from django.contrib.auth import get_user_model
from publications.models import BlockedEmail, BlockedDomain
from time import sleep

User = get_user_model()

class AdminBlockUserTests(TransactionTestCase):

    def setUp(self):
        self.superuser, created = User.objects.get_or_create(
            username="admin",
            email="admin@example.com",
            defaults={"is_staff": True, "is_superuser": True}
        )
        if created:
            self.superuser.set_password("admin123") 
            self.superuser.save()

        self.test_user, created = User.objects.get_or_create(
            username="testuser",
            email="blocked@test.com"
        )
        if created:
            self.test_user.set_password("password123")
            self.test_user.save()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.browser = start_firefox("http://localhost:8000/admin/", headless=False)

    @classmethod
    def tearDownClass(cls):
        kill_browser()
        super().tearDownClass()

    def test_admin_can_block_user(self):

        write(self.superuser.username, into="Username") 
        write("admin123", into="Password") 
        click("Log in")

        go_to("http://localhost:8000/admin/auth/user/")
        
        testuser_link = find_all(Link("testuser"))  

        if testuser_link:
            row = testuser_link[0].web_element.find_element("xpath", "./ancestor::tr")  
            checkbox = row.find_element("xpath", ".//input[@type='checkbox']") 
            checkbox.click()  

        click("Action:")
        self.browser.execute_script("""
            let actionDropdown = document.querySelector("select[name='action']");
            actionDropdown.value = "delete_user_and_block";
        """)

        click("Go")

        sleep(1)

        self.assertFalse(User.objects.filter(email="blocked@test.com").exists())
        self.assertTrue(BlockedEmail.objects.filter(email="blocked@test.com").exists())
        self.assertTrue(BlockedDomain.objects.filter(domain="test.com").exists())

