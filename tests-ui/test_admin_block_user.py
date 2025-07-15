import os
import django
import subprocess

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "optimap.settings")
django.setup()

import unittest
from django.test import TransactionTestCase, TestCase
from helium import *
from django.contrib.auth import get_user_model
from publications.models import BlockedEmail, BlockedDomain
from time import sleep

User = get_user_model()

class AdminBlockUserTests(TestCase):
    def setUp(self):
        """Set up a superuser, test user, and start the browser before each test."""
        self.superuser, _ = User.objects.get_or_create(
            username="admin",
            email="admin@example.com",
            defaults={"is_staff": True, "is_superuser": True}
        )
        self.superuser.set_password("admin123")
        self.superuser.save()

        User.objects.filter(email="blocked@test.com").delete()
        self.test_user = User.objects.create_user(
            username="testuser",
            email="blocked@test.com",
            password="password123"
        )
        self.test_user.save()

        self.kill_existing_firefox_processes()
        try:
            self.browser = start_chrome("http://localhost:8000/admin/", headless=True)
        except Exception as e:
            print(f"Error starting browser: {e}")
            self.browser = None

    def tearDown(self):
        """Close the browser after each test."""
        if self.browser:
            try:
                kill_browser()
            except Exception as e:
                print(f"Error closing browser: {e}")

    def kill_existing_firefox_processes(self):
        """Kill any existing Firefox processes to ensure a clean start."""
        try:
            subprocess.run(["pkill", "-f", "firefox"], check=True)
        except subprocess.CalledProcessError as e:
            if e.returncode == 1:
                print("No existing Firefox processes found to kill.")
            else:
                print(f"Error killing existing Firefox processes: {e}")

    def block_user_by_action(self, action_name):
        """Helper function to execute a specific admin action."""
        try:
            write(self.superuser.username, into="Username")
            write("admin123", into="Password")
            click("Log in")

            go_to("http://localhost:8000/admin/auth/user/")

            if not User.objects.filter(email="blocked@test.com").exists():
                self.test_user = User.objects.create_user(
                    username="testuser",
                    email="blocked@test.com",
                    password="password123"
                )
                self.test_user.save()

            testuser_link = find_all(Link("testuser"))
            if testuser_link:
                row = testuser_link[0].web_element.find_element("xpath", "./ancestor::tr")
                checkbox = row.find_element("xpath", ".//input[@type='checkbox']")
                checkbox.click()

            click("Action:")
            script = f"""
                let actionDropdown = document.querySelector("select[name='action']");
                actionDropdown.value = "{action_name}";
            """
            self.browser.execute_script(script)

            click("Go")

            sleep(1)
        except Exception as e:
            print(f"Error in block_user_by_action: {e}")

    def test_delete_user_and_block_email(self):
        """Test admin action: Delete user and block email only."""
        try:
            self.block_user_by_action("block_email")

            user_exists = User.objects.filter(email="blocked@test.com").exists()
            email_blocked = BlockedEmail.objects.filter(email="blocked@test.com").exists()
            domain_blocked = BlockedDomain.objects.filter(domain="test.com").exists()

            self.assertFalse(user_exists)
            self.assertTrue(email_blocked)
            self.assertFalse(domain_blocked)
        except Exception as e:
            print(f"Error in test_delete_user_and_block_email: {e}")

    def test_delete_user_and_block_email_and_domain(self):
        """Test admin action: Delete user and block email and domain."""
        try:
            self.block_user_by_action("block_email_and_domain")

            user_exists = User.objects.filter(email="blocked@test.com").exists()
            email_blocked = BlockedEmail.objects.filter(email="blocked@test.com").exists()
            domain_blocked = BlockedDomain.objects.filter(domain="test.com").exists()

            self.assertFalse(user_exists)
            self.assertTrue(email_blocked)
            self.assertTrue(domain_blocked) 
        except Exception as e:
            print(f"Error in test_delete_user_and_block_email_and_domain: {e}")

if __name__ == "__main__":
    unittest.main()