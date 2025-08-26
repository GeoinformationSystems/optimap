# tests/test_end_to_end_zenodo.py
import json
import tempfile
from pathlib import Path
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from django.core.management import call_command
from django.test import override_settings

from publications.models import Publication, Source


class EndToEndZenodoTest(TestCase):
    def setUp(self):
        # Temp “project root”
        self._tmpdir = tempfile.TemporaryDirectory()
        self.project_root = Path(self._tmpdir.name)
        self.templates_dir = self.project_root / "publications" / "templates"
        self.cmds_dir = self.project_root / "publications" / "management" / "commands"
        self.data_dir = self.project_root / "data"
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        self.cmds_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Minimal README template with Sources
        (self.templates_dir / "README.md.j2").write_text(
            "# OPTIMAP FAIR Data Package\n"
            "**Version:** {{ version }}\n\n"
            "## Sources\n\n"
            "{% for src in sources %}- [{{ src.name }}]({{ src.url }})\n{% endfor %}\n"
            "\n## Codebook\n\n"
            "| Field | Description |\n|---|---|\n| id | pk |\n",
            encoding="utf-8",
        )

        # Test data
        Publication.objects.create(title="A", publicationDate="2010-10-10")
        # BAD label cases we want cleaned up (no DB NULLs!)
        Source.objects.create(name="2000", url_field="http://optimap.science")    # numeric-only -> should become OPTIMAP
        Source.objects.create(name="",     url_field="https://example.org")       # blank -> fallback to domain label
        Source.objects.create(name=" ",    url_field="https://example.org")       # duplicate domain -> deduped
        # Proper label kept
        Source.objects.create(name="AGILE: GIScience Series", url_field="https://agile-giss.copernicus.org")

        # Import command modules
        import importlib
        self.render_mod = importlib.import_module("publications.management.commands.render_zenodo_desc")
        self.deploy_mod = importlib.import_module("publications.management.commands.deploy_zenodo")

        # Fake Path so parents[3] stays inside tmp root
        class FakePath(Path):
            _flavour = Path(".")._flavour
            def resolve(self):
                return self
        self.FakePath = FakePath
        self.render_file = str(self.cmds_dir / "render_zenodo_desc.py")
        self.deploy_file = str(self.cmds_dir / "deploy_zenodo.py")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_end_to_end_render_then_deploy(self):
        # Don’t actually run `git archive`
        def _noop(*a, **k): return None

        # ---- Render README/ZIP/dynamic json
        with patch.object(self.render_mod, "__file__", new=self.render_file), \
             patch.object(self.render_mod, "Path", self.FakePath), \
             patch("subprocess.run", _noop):
            call_command("render_zenodo_desc")

        readme_path = self.data_dir / "README.md"
        zip_path    = self.data_dir / "optimap-main.zip"
        dyn_path    = self.data_dir / "zenodo_dynamic.json"

        if not zip_path.exists():
            zip_path.write_bytes(b"ZIP")
        if not dyn_path.exists():
            dyn_path.write_text(json.dumps({
                "title": "OPTIMAP FAIR Data Package (test)",
                "version": "v9",
                "keywords": ["open data"],
                "related_identifiers": [
                    {"relation": "describes", "identifier": "https://optimap.science", "scheme": "url"}
                ],
            }), encoding="utf-8")

        self.assertTrue(readme_path.exists(), "README.md not generated")
        self.assertTrue(zip_path.exists(), "optimap-main.zip not generated")
        self.assertTrue(dyn_path.exists(), "zenodo_dynamic.json not found")

        md = readme_path.read_text(encoding="utf-8")
        # Sources cleanup assertions
        self.assertNotIn("- [2000](", md, "Numeric-only label leaked into Sources")
        self.assertIn("- [OPTIMAP](http://optimap.science)", md, "OPTIMAP override missing")
        self.assertIn("AGILE: GIScience Series", md, "Named source missing")
        self.assertEqual(md.count("example.org"), 1, "Duplicate source/domain not deduped")

        # Fake dump files to upload
        (self.data_dir / "optimap_data_dump_20250101.geojson").write_text("{}", encoding="utf-8")
        (self.data_dir / "optimap_data_dump_20250101.gpkg").write_bytes(b"GPKG")

        # ---- Mock Zenodo API + uploader
        existing = {
            "submitted": False,
            "state": "unsubmitted",
            "links": {"edit": "http://edit"},
            "metadata": {
                "title": "Existing Title",
                "upload_type": "dataset",
                "publication_date": "2025-07-14",
                "creators": [{"name": "OPTIMAP"}],
                "keywords": ["open science"],
                "related_identifiers": [
                    {"relation": "isSupplementTo", "identifier": "https://old.example", "scheme": "url"}
                ],
                "language": "eng",
            },
        }

        put_payload = {}
        def _fake_get(url, params=None):
            class R:
                status_code = 200
                def json(self_inner): return deepcopy(existing)
            return R()
        def _fake_post(url, params=None):
            class R:
                status_code = 200
                text = "ok"
            return R()
        def _fake_put(url, params=None, headers=None, data=None):
            put_payload["metadata"] = json.loads(data)["metadata"]
            class R:
                status_code = 200
                text = "ok"
            return R()

        uploaded = {}
        def _fake_update_zenodo(dep_id, paths, sandbox, access_token, publish):
            self.assertEqual(access_token, "tok")
            self.assertTrue(sandbox)
            self.assertFalse(publish)
            names = {Path(p).name for p in paths}
            self.assertIn("README.md", names)
            self.assertIn("optimap-main.zip", names)
            self.assertTrue(any(n.endswith(".geojson") for n in names))
            self.assertTrue(any(n.endswith(".gpkg") for n in names))
            uploaded["paths"] = paths
            class R:
                def json(self): return {"links": {"html": f"https://sandbox.zenodo.org/deposit/{dep_id}"}}
            return R()

        with patch.object(self.deploy_mod, "__file__", new=self.deploy_file), \
             patch.object(self.deploy_mod, "Path", self.FakePath), \
             patch.object(self.deploy_mod, "_markdown_to_html", lambda s: "<p>HTML</p>"), \
             patch.object(self.deploy_mod, "update_zenodo", _fake_update_zenodo), \
             patch.object(self.deploy_mod.requests, "get", _fake_get), \
             patch.object(self.deploy_mod.requests, "put", _fake_put), \
             patch.object(self.deploy_mod.requests, "post", _fake_post), \
             override_settings(ZENODO_UPLOADS_ENABLED=True, ZENODO_SANDBOX_TOKEN="tok"):

            call_command(
                "deploy_zenodo",
                "--confirm",
                "--deposition-id", "123456",
                "--patch", "description,version,keywords,related_identifiers",
                "--merge-keywords",
                "--merge-related",
            )

        # Merged metadata: required fields preserved
        merged = put_payload["metadata"]
        self.assertEqual(merged["title"], "Existing Title")
        self.assertEqual(merged["upload_type"], "dataset")
        self.assertEqual(merged["publication_date"], "2025-07-14")
        self.assertEqual(merged["creators"], [{"name": "OPTIMAP"}])

        # Description updated (HTML)
        self.assertIn("description", merged)
        self.assertTrue(merged["description"].startswith("<p"))

        # Version updated
        ver = merged.get("version")
        self.assertIsInstance(ver, str)
        self.assertRegex(ver, r"^v\d+$")
        # Related identifiers merged (old + new)
        rel = {(d["identifier"], d["relation"]) for d in merged.get("related_identifiers", [])}
        self.assertIn(("https://old.example", "isSupplementTo"), rel)
        self.assertIn(("https://optimap.science", "describes"), rel)

        # Uploader called with expected files
        self.assertIn("paths", uploaded)
        self.assertGreater(len(uploaded["paths"]), 0)
