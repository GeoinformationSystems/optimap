import os
import django
import unittest
from helium import *
from time import sleep
from django.core.cache import cache
from django.contrib.auth import get_user_model

# Ensure Django settings are configured
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

User = get_user_model()

class AccountDeletionUITest(unittest.TestCase):
    def setUp(self):
        """Set up the test user and start browser"""
        self.email = "testuser@example.com"
        self.token = "mock-token-12345" 
        self.delete_token = "mock-delete-token-67890"  
        
        User.objects.filter(email=self.email).delete()

        self.user = User.objects.create_user(username=self.email, email=self.email, password="password")

        cache.set(self.token, self.email, timeout=300)  
        cache.set(f"user_delete_token_{self.delete_token}", self.user.id, timeout=600)  

        # Start browser
        self.browser = start_firefox("http://localhost:8000", headless=False)

    def test_delete_account(self):

        click(S('#navbarDarkDropdown1'))

        write(self.email,  into='email')
        click(S('button[type="submit"]'))

        go_to(f"http://localhost:8000/login/{self.token}")  
        sleep(3)

        go_to("http://localhost:8000/usersettings/")
        sleep(2)

        click("Delete account")
        sleep(1)

        click("Delete")
        sleep(3)

        go_to(f"http://localhost:8000/confirm-delete/{self.delete_token}")
        sleep(3)

        click("Permanently Delete Account")
        sleep(3)

        user = User.objects.filter(email=self.email).first()
        self.assertIsNone(user, "User was not deleted from the database!")

    def tearDown(self):
        """Close browser after test"""
        if self.browser:
            kill_browser()

if __name__ == "__main__":
    unittest.main()
