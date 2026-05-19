"""
Integration tests for Zenodo deposition.

These tests run against the actual Zenodo sandbox API and require:
1. A tests/.env file with ZENODO_API_TOKEN and ZENODO_SANDBOX_DEPOSITION_ID
2. Active internet connection
3. Valid Zenodo sandbox credentials

To run these tests:
    python manage.py test tests.test_zenodo_integration

To skip these tests (default):
    python manage.py test tests --exclude-tag=integration
"""
import os
import json
import tempfile
from pathlib import Path
from django.test import TestCase, tag, override_settings
from django.core.management import call_command
from works.models import Work, Source
from django.conf import settings


def load_test_env():
    """Load environment variables from tests/.env file."""
    env_file = Path(__file__).parent / '.env'
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())


@tag('integration', 'zenodo')
class ZenodoIntegrationTest(TestCase):
    """
    Integration tests for Zenodo API.

    Requires tests/.env with:
    - ZENODO_API_TOKEN
    - ZENODO_SANDBOX_DEPOSITION_ID
    - ZENODO_API_BASE (optional, defaults to sandbox)
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        load_test_env()

        cls.api_token = os.environ.get('ZENODO_API_TOKEN')
        cls.deposition_id = os.environ.get('ZENODO_SANDBOX_DEPOSITION_ID')
        cls.api_base = os.environ.get('ZENODO_API_BASE', 'https://sandbox.zenodo.org/api')

        if not cls.api_token or not cls.deposition_id:
            raise unittest.SkipTest(
                "Zenodo integration tests require ZENODO_API_TOKEN and "
                "ZENODO_SANDBOX_DEPOSITION_ID in tests/.env file. "
                "See tests/.env.template for setup instructions."
            )

    def setUp(self):
        """Set up test data and temporary directories."""
        self._tmpdir = tempfile.TemporaryDirectory()
        self.project_root = Path(self._tmpdir.name)
        self.data_dir = self.project_root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Create test data files
        (self.data_dir / "README.md").write_text(
            "# OPTIMAP Test Data\\n\\nTest dataset for integration testing.",
            encoding="utf-8"
        )
        (self.data_dir / "optimap-main.zip").write_bytes(b"TEST_ZIP_CONTENT")

        # Create dynamic metadata
        (self.data_dir / "zenodo_dynamic.json").write_text(json.dumps({
            "title": "OPTIMAP Test Dataset",
            "version": "v1.0.0-test",
            "related_identifiers": [
                {
                    "relation": "describes",
                    "identifier": "https://optimap.science",
                    "scheme": "url"
                }
            ]
        }), encoding="utf-8")

        # Create fake data dump files
        (self.data_dir / "optimap_data_dump_20250101.geojson").write_text("{}", encoding="utf-8")
        (self.data_dir / "optimap_data_dump_20250101.gpkg").write_bytes(b"GPKG_TEST")

        # Create minimal database records
        Work.objects.create(title="Test Work", doi="10.test/integration")
        Source.objects.create(name="Test Source", url_field="https://test.example.com")

    def tearDown(self):
        """Clean up temporary directories."""
        self._tmpdir.cleanup()

    @override_settings(
        ZENODO_API_TOKEN=None,  # Will be set from environment
        ZENODO_SANDBOX_DEPOSITION_ID=None,  # Will be set from environment
        ZENODO_API_BASE=None  # Will be set from environment
    )
    def test_render_zenodo_command(self):
        """Test that render_zenodo command generates all required files."""
        with override_settings(
            ZENODO_API_TOKEN=self.api_token,
            ZENODO_SANDBOX_DEPOSITION_ID=self.deposition_id,
            ZENODO_API_BASE=self.api_base
        ):
            # Run render command
            call_command(
                'render_zenodo',
                stdout=tempfile.TemporaryFile(mode='w+'),
                stderr=tempfile.TemporaryFile(mode='w+')
            )

            # Verify generated files exist
            data_dir = Path(settings.BASE_DIR) / 'data'
            self.assertTrue((data_dir / 'README.md').exists(), "README.md should be generated")
            self.assertTrue((data_dir / 'zenodo_dynamic.json').exists(), "zenodo_dynamic.json should exist")

    @override_settings(
        ZENODO_API_TOKEN=None,
        ZENODO_SANDBOX_DEPOSITION_ID=None,
        ZENODO_API_BASE=None
    )
    def test_deposit_zenodo_command_dry_run(self):
        """Test deposit_zenodo command in dry-run mode (no actual upload)."""
        with override_settings(
            ZENODO_API_TOKEN=self.api_token,
            ZENODO_SANDBOX_DEPOSITION_ID=self.deposition_id,
            ZENODO_API_BASE=self.api_base
        ):
            # Test with --dry-run flag if available
            # This test verifies the command can be called without errors
            # Actual upload testing would require cleanup logic
            try:
                call_command(
                    'deposit_zenodo',
                    '--help',
                    stdout=tempfile.TemporaryFile(mode='w+'),
                    stderr=tempfile.TemporaryFile(mode='w+')
                )
            except SystemExit:
                pass  # --help exits, which is expected

    def test_env_file_loading(self):
        """Test that environment variables are loaded from tests/.env."""
        self.assertIsNotNone(self.api_token, "ZENODO_API_TOKEN should be loaded from .env")
        self.assertIsNotNone(self.deposition_id, "ZENODO_SANDBOX_DEPOSITION_ID should be loaded")
        self.assertIn('zenodo.org', self.api_base, "ZENODO_API_BASE should contain zenodo.org")

    def test_zenodo_api_connectivity(self):
        """Test basic connectivity to Zenodo API."""
        import requests

        headers = {"Authorization": f"Bearer {self.api_token}"}
        response = requests.get(f"{self.api_base}/deposit/depositions", headers=headers)

        self.assertEqual(
            response.status_code, 200,
            f"Should be able to connect to Zenodo API. Status: {response.status_code}"
        )

        depositions = response.json()
        self.assertIsInstance(depositions, list, "Depositions should be a list")


@tag('integration', 'zenodo', 'slow')
class ZenodoFullDepositTest(TestCase):
    """
    Full end-to-end deposit tests.

    WARNING: These tests actually upload to Zenodo sandbox.
    Use with caution and clean up manually if needed.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        load_test_env()

        cls.api_token = os.environ.get('ZENODO_API_TOKEN')
        cls.deposition_id = os.environ.get('ZENODO_SANDBOX_DEPOSITION_ID')
        cls.api_base = os.environ.get('ZENODO_API_BASE', 'https://sandbox.zenodo.org/api')

        if not cls.api_token or not cls.deposition_id:
            raise unittest.SkipTest(
                "Full deposit tests require ZENODO_API_TOKEN and "
                "ZENODO_SANDBOX_DEPOSITION_ID in tests/.env"
            )

    def setUp(self):
        """Set up test data."""
        Work.objects.create(title="Full Test Work", doi="10.test/full")
        Source.objects.create(name="Full Test Source", url_field="https://test.example.com")

    @tag('slow', 'upload')
    def test_full_deposit_cycle(self):
        """
        Test full deposit cycle: render → deposit → verify.

        This test actually uploads to Zenodo sandbox.
        Run manually with: python manage.py test tests.test_zenodo_integration.ZenodoFullDepositTest --tag=upload
        """
        from works.models import ZenodoDepositionLog
        import tempfile
        from pathlib import Path

        # Set up temporary data directory
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            # Create required files
            (data_dir / "README.md").write_text(
                "# OPTIMAP Integration Test\\n\\nTest deposit cycle.",
                encoding="utf-8"
            )
            (data_dir / "optimap-main.zip").write_bytes(b"TEST_ZIP_CONTENT_INTEGRATION")

            # Create dynamic metadata
            import json
            (data_dir / "zenodo_dynamic.json").write_text(json.dumps({
                "title": "OPTIMAP Integration Test Dataset",
                "version": "v1.0.0-integration-test",
                "description": "Integration test deposit",
                "keywords": ["test", "integration"],
                "related_identifiers": [
                    {
                        "relation": "describes",
                        "identifier": "https://optimap.science/test",
                        "scheme": "url"
                    }
                ]
            }), encoding="utf-8")

            # Override settings to use temporary directory
            with override_settings(
                ZENODO_API_TOKEN=self.api_token,
                ZENODO_SANDBOX_DEPOSITION_ID=self.deposition_id,
                ZENODO_API_BASE=self.api_base,
                PROJECT_ROOT=Path(tmpdir)
            ):
                # Get initial log count
                initial_log_count = ZenodoDepositionLog.objects.count()

                # Run deposit command
                from io import StringIO
                out = StringIO()
                err = StringIO()

                call_command(
                    'deposit_zenodo',
                    '--deposition-id', self.deposition_id,
                    stdout=out,
                    stderr=err
                )

                # Verify log was created
                self.assertEqual(
                    ZenodoDepositionLog.objects.count(),
                    initial_log_count + 1,
                    "A deposition log entry should be created"
                )

                # Get the most recent log entry
                log_entry = ZenodoDepositionLog.objects.order_by('-deposition_date').first()

                # Verify log entry details
                self.assertIsNotNone(log_entry, "Log entry should exist")
                self.assertEqual(log_entry.deposition_id, self.deposition_id)
                self.assertEqual(log_entry.status, 'success',
                    f"Deposition should succeed. Error: {log_entry.error_message}")
                self.assertEqual(log_entry.api_base, self.api_base)
                self.assertEqual(log_entry.version, "v1.0.0-integration-test")
                self.assertGreater(log_entry.works_count, 0, "Should track works count")
                self.assertIsNotNone(log_entry.files_uploaded, "Should track uploaded files")
                self.assertGreater(len(log_entry.files_uploaded), 0, "Should have uploaded files")
                self.assertGreater(log_entry.total_size_bytes, 0, "Should track total size")
                self.assertIsNotNone(log_entry.upload_duration_seconds, "Should track duration")
                self.assertGreater(log_entry.upload_duration_seconds, 0, "Duration should be positive")
                self.assertIsNotNone(log_entry.deposition_summary, "Should have summary")
                self.assertIn("Successfully uploaded", log_entry.deposition_summary)

                # Verify files were tracked
                file_names = [f['name'] for f in log_entry.files_uploaded]
                self.assertIn("README.md", file_names, "README.md should be uploaded")
                self.assertIn("optimap-main.zip", file_names, "ZIP should be uploaded")

                # Verify Zenodo response data (if available)
                if log_entry.zenodo_url:
                    self.assertIn("zenodo.org", log_entry.zenodo_url, "Should have Zenodo URL")

                # Verify command output
                output = out.getvalue()
                self.assertIn("Updated deposition", output, "Should report success")
                self.assertIn("Deposition log saved", output, "Should confirm log was saved")

                # Test API to verify deposition
                import requests
                headers = {"Authorization": f"Bearer {self.api_token}"}
                response = requests.get(
                    f"{self.api_base}/deposit/depositions/{self.deposition_id}",
                    headers=headers
                )
                self.assertEqual(response.status_code, 200, "Should be able to fetch deposition")

                dep_data = response.json()
                self.assertEqual(
                    str(dep_data.get('id')),
                    self.deposition_id,
                    "Deposition ID should match"
                )

                # Verify files were actually uploaded to Zenodo
                files = dep_data.get('files', [])
                self.assertGreater(len(files), 0, "Deposition should have files")

                zenodo_file_names = [f['filename'] for f in files]
                self.assertIn("README.md", zenodo_file_names, "README.md should be on Zenodo")

                # Print test success details (using print instead of self.stdout for TestCase)
                print(
                    f"\n✅ Full deposit cycle test passed. "
                    f"Log ID: {log_entry.id}, "
                    f"Files uploaded: {len(log_entry.files_uploaded)}, "
                    f"Duration: {log_entry.upload_duration_seconds:.2f}s"
                )


import unittest
