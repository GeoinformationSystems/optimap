# tests/test_deposit_zenodo.py
import json
import tempfile
from pathlib import Path
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from django.core.management import call_command
from django.test import override_settings
from publications.models import Publication, Source


class DepositZenodoTest(TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.project_root = Path(self._tmpdir.name)
        self.templates_dir = self.project_root / "publications" / "templates"
        self.cmds_dir = self.project_root / "publications" / "management" / "commands"
        self.data_dir = self.project_root / "data"
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        self.cmds_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Minimal README so description→HTML works
        (self.data_dir / "README.md").write_text("# Title\n\nSome text.", encoding="utf-8")
        (self.data_dir / "optimap-main.zip").write_bytes(b"ZIP")
        # dynamic JSON with new related identifiers and version
        (self.data_dir / "zenodo_dynamic.json").write_text(json.dumps({
            "title": "OPTIMAP FAIR Data Package (test)",
            "version": "v999",
            "related_identifiers": [
                {"relation": "describes", "identifier": "https://optimap.science", "scheme": "url"}
            ]
        }), encoding="utf-8")

        # Fake dump files to upload
        (self.data_dir / "optimap_data_dump_20250101.geojson").write_text("{}", encoding="utf-8")
        (self.data_dir / "optimap_data_dump_20250101.gpkg").write_bytes(b"GPKG")

        # Minimal DB so import paths work
        Publication.objects.create(title="A", publicationDate="2010-10-10")
        Source.objects.create(name="OPTIMAP", url_field="https://optimap.science")

        # Command import – prefer deposit_zenodo; fallback to deploy_zenodo if needed
        import importlib
        try:
            self.deposit_mod = importlib.import_module(
                "publications.management.commands.deposit_zenodo"
            )
        except ModuleNotFoundError:
            self.deposit_mod = importlib.import_module(
                "publications.management.commands.deploy_zenodo"
            )

        class FakePath(Path):
            _flavour = Path(".")._flavour
            def resolve(self):
                return self
        self.FakePath = FakePath
        self.deposit_file = str(self.cmds_dir / "deposit_zenodo.py")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_deposit_merges_metadata_and_uses_zenodo_client_for_uploads(self):
        # Fake Zenodo deposition (existing metadata)
        existing = {
            "submitted": False,
            "state": "unsubmitted",
            "links": {"edit": "http://edit", "bucket": "http://bucket"},
            "metadata": {
                "title": "Existing Title",
                "upload_type": "dataset",
                "publication_date": "2025-07-14",
                "creators": [{"name": "OPTIMAP"}],
                "keywords": ["Open Science"],
                "related_identifiers": [
                    {"relation": "isSupplementTo", "identifier": "https://old.example", "scheme": "url"}
                ],
                "language": "eng",
                "description": "<p>Old</p>",
                "version": "v1",
            },
        }

        put_payload = {}

    def _fake_get(url, params=None, **kwargs):
        class R:
            status_code = 200
            text = "ok"
            def json(self):
                # whatever object your test expects (e.g., deepcopy(existing))
                return deepcopy(existing)
            def raise_for_status(self):
                return None
        return R()

    def _fake_post(url, params=None, json=None, **kwargs):
        class R:
            status_code = 200
            text = "ok"
            def json(self):
                # return what your code reads from POST responses, if anything
                return {"links": {"bucket": "https://example-bucket"}}
            def raise_for_status(self):
                return None
        return R()

    def _fake_put(url, params=None, data=None, headers=None, **kwargs):
        class R:
            status_code = 200
            text = "ok"
            def raise_for_status(self):
                return None
        return R()

        uploaded = {}

        # zenodo-client upload shim: capture files that would be uploaded
        def _fake_update_zenodo(deposition_id, paths, sandbox=True, access_token=None, publish=False):
            self.assertEqual(deposition_id, "123456")
            self.assertTrue(sandbox)
            self.assertEqual(access_token, "tok")
            names = {Path(p).name for p in paths}
            self.assertIn("README.md", names)
            self.assertIn("optimap-main.zip", names)
            self.assertTrue(any(n.endswith(".geojson") for n in names))
            self.assertTrue(any(n.endswith(".gpkg") for n in names))
            uploaded["paths"] = [str(p) for p in paths]
            class R:
                def json(self): return {"links": {"html": f"https://sandbox.zenodo.org/deposit/{deposition_id}"}}
            return R()

        with patch.object(self.deposit_mod, "__file__", new=self.deposit_file), \
             patch.object(self.deposit_mod, "Path", self.FakePath), \
             patch.object(self.deposit_mod.requests, "get", _fake_get), \
             patch.object(self.deposit_mod.requests, "put", _fake_put), \
             patch.object(self.deposit_mod, "update_zenodo", _fake_update_zenodo), \
             patch.object(self.deposit_mod, "_markdown_to_html", lambda s: "<p>HTML</p>"), \
             override_settings(ZENODO_UPLOADS_ENABLED=True):

            call_command(
                "deposit_zenodo",
                "--deposition-id", "123456",
            )

        # Merged metadata: required fields preserved, description/version updated, related merged
        merged = put_payload["metadata"]
        self.assertEqual(merged["title"], "Existing Title")
        self.assertEqual(merged["upload_type"], "dataset")
        self.assertEqual(merged["publication_date"], "2025-07-14")
        self.assertEqual(merged["creators"], [{"name": "OPTIMAP"}])

        self.assertIn("description", merged)
        self.assertTrue(merged["description"].startswith("<p"))  # from markdown->HTML

        self.assertIsInstance(merged.get("version"), str)
        rel = {(d["identifier"], d["relation"]) for d in merged.get("related_identifiers", [])}
        self.assertIn(("https://old.example", "isSupplementTo"), rel)
        self.assertIn(("https://optimap.science", "describes"), rel)

        # Uploader called with expected files
        self.assertIn("paths", uploaded)
        self.assertGreater(len(uploaded["paths"]), 0)
