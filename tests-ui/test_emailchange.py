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

class EmailChangeUITest(unittest.TestCase):
    def setUp(self):
        """Set up the test user and start browser"""
        self.old_email = "testuser@example.com"
        self.new_email = "newemail@example.com"
        self.token = "mock-token-12345" 
        self.change_token = "mock-change-token-67890"  

        User.objects.filter(email=self.old_email).delete()
        User.objects.filter(email=self.new_email).delete()

        self.user = User.objects.create_user(username=self.old_email, email=self.old_email, password="password")
        self.user.save()

        cache.set(self.token, self.old_email, timeout=300)  
        cache.set(f"email_confirmation_{self.new_email}", 
            {"token": self.change_token, "old_email": self.old_email}, 
            timeout=600
        )

        self.browser = start_firefox("http://localhost:8000", headless=False)

    def test_email_change_process(self):
        """Test the full email change UI process"""

        click(S('#navbarDarkDropdown1'))

        write(self.old_email,  into='email')
        click(S('button[type="submit"]'))

        sleep(1)
        
        go_to(f"http://localhost:8000/login/{self.token}")  
        sleep(3)

        go_to("http://localhost:8000/usersettings/")
        sleep(2)

        click("Change Email")

        write(self.new_email, into="Enter your new email")
        sleep(1)
        click("Save Changes")
        sleep(5)

        stored_data = cache.get(f"email_confirmation_{self.new_email}")

        if stored_data and "token" in stored_data:
            correct_token = stored_data["token"]
            confirmation_url = f"http://localhost:8000/confirm-email/{correct_token}/{self.new_email}"
            go_to(confirmation_url)
            sleep(5)
        else:
            assert False, "Test Failed: Email confirmation token not found in cache!"

        self.user.refresh_from_db()
        assert self.user.email == self.new_email, "Email was not updated in the database!"

    def tearDown(self):
        """Close browser after test"""
        if self.browser:
            kill_browser()

if __name__ == "__main__":
    unittest.main()
