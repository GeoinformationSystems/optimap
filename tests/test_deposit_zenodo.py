# tests/test_deposit_zenodo.py
import json
import tempfile
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, SimpleTestCase, override_settings
from works.models import Work, Source, ZenodoDepositionLog
from works.zenodo import _build_upload_list, _latest_dump_files


class BuildUploadListTest(SimpleTestCase):
    """Direct unit tests for the upload-list helpers (issue #63, item 4)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.data_dir = self.root / "data"
        self.dump_dir = self.root / "optimap_cache"
        self.data_dir.mkdir()
        self.dump_dir.mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_latest_dump_files_picks_newest_timestamp_only(self):
        # Two cycles in the same dir, three formats each
        for ts in ("20240101", "20250101"):
            (self.dump_dir / f"optimap_data_dump_{ts}.geojson").write_text("{}")
            (self.dump_dir / f"optimap_data_dump_{ts}.geojson.gz").write_bytes(b"\x1f\x8b")
            (self.dump_dir / f"optimap_data_dump_{ts}.gpkg").write_bytes(b"GPKG")
        # And a CSV pair for the newer cycle only
        (self.dump_dir / "optimap_data_dump_20250101.csv").write_text("a,b\n")
        (self.dump_dir / "optimap_data_dump_20250101.csv.gz").write_bytes(b"\x1f\x8b")

        files = _latest_dump_files(self.dump_dir)
        names = {p.name for p in files}
        self.assertEqual(names, {
            "optimap_data_dump_20250101.geojson",
            "optimap_data_dump_20250101.geojson.gz",
            "optimap_data_dump_20250101.gpkg",
            "optimap_data_dump_20250101.csv",
            "optimap_data_dump_20250101.csv.gz",
        })

    def test_build_upload_list_includes_csv_variants(self):
        (self.data_dir / "README.md").write_text("# x")
        (self.data_dir / "optimap-main.zip").write_bytes(b"ZIP")
        for ext in ("geojson", "geojson.gz", "gpkg", "csv", "csv.gz"):
            (self.data_dir / f"optimap_data_dump_20250101.{ext}").write_bytes(b"x")

        paths = _build_upload_list(self.data_dir, dump_dir=self.dump_dir)
        names = {p.name for p in paths}

        # README + git archive snapshot
        self.assertIn("README.md", names)
        self.assertIn("optimap-main.zip", names)
        # All five dump formats land in the upload
        for ext in ("geojson", "geojson.gz", "gpkg", "csv", "csv.gz"):
            self.assertIn(f"optimap_data_dump_20250101.{ext}", names)

    def test_build_upload_list_falls_back_to_dump_dir_when_data_dir_has_no_dumps(self):
        """Production layout: render writes to data/, regenerate writes to cache."""
        (self.data_dir / "README.md").write_text("# x")
        (self.data_dir / "optimap-main.zip").write_bytes(b"ZIP")
        # Dumps only in dump_dir
        for ext in ("geojson", "gpkg", "csv"):
            (self.dump_dir / f"optimap_data_dump_20250101.{ext}").write_bytes(b"x")

        paths = _build_upload_list(self.data_dir, dump_dir=self.dump_dir)
        names = {p.name for p in paths}
        self.assertIn("README.md", names)
        self.assertIn("optimap_data_dump_20250101.geojson", names)
        self.assertIn("optimap_data_dump_20250101.gpkg", names)
        self.assertIn("optimap_data_dump_20250101.csv", names)


class DepositZenodoTest(TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.project_root = Path(self._tmpdir.name)
        self.templates_dir = self.project_root / "works" / "templates"
        self.cmds_dir = self.project_root / "works" / "management" / "commands"
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
        Work.objects.create(title="A", publicationDate="2010-10-10")
        Source.objects.create(name="OPTIMAP", url_field="https://optimap.science")

        # Import zenodo module
        import importlib
        self.zenodo_mod = importlib.import_module("works.zenodo")

        class FakePath(Path):
            _flavour = Path(".")._flavour
            def resolve(self):
                return self
        self.FakePath = FakePath
        self.zenodo_file = str(self.project_root / "works" / "zenodo.py")

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

        # Mock Zenodo client
        mock_zenodo = type('MockZenodo', (), {
            'access_token': None,
            'update': lambda *args, **kwargs: _fake_update_zenodo(**kwargs)
        })()

        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch.object(self.zenodo_mod.requests, "get", _fake_get), \
             patch.object(self.zenodo_mod.requests, "put", _fake_put), \
             patch.object(self.zenodo_mod.requests, "delete", lambda *a, **k: type('R', (), {'status_code': 204})()), \
             patch.object(self.zenodo_mod, "Zenodo", return_value=mock_zenodo), \
             patch.object(self.zenodo_mod, "_markdown_to_html", lambda s: "<p>HTML</p>"), \
             override_settings(ZENODO_UPLOADS_ENABLED=True, ZENODO_API_TOKEN="tok", ZENODO_SANDBOX_DEPOSITION_ID="123456"):

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

    def test_doi_fields_are_protected_from_overwrite(self):
        """Test that DOI and prereserve_doi fields are never overwritten."""
        # Existing deposition with reserved DOI
        existing_with_doi = {
            "submitted": False,
            "state": "unsubmitted",
            "links": {"edit": "http://edit", "bucket": "http://bucket"},
            "metadata": {
                "title": "Test Title",
                "upload_type": "dataset",
                "publication_date": "2025-01-01",
                "creators": [{"name": "Test Author"}],
                "doi": "10.5072/zenodo.123456",
                "prereserve_doi": {"doi": "10.5072/zenodo.123456", "recid": 123456},
                "version": "v1",
                "description": "<p>Old description</p>",
            },
        }

        captured_metadata = {}

        def _fake_get(url, params=None, **kwargs):
            class R:
                status_code = 200
                text = "ok"
                def json(self):
                    return deepcopy(existing_with_doi)
                def raise_for_status(self):
                    return None
            return R()

        def _fake_put(url, params=None, data=None, headers=None, **kwargs):
            # Capture the metadata that would be sent to Zenodo
            if data:
                captured_metadata.update(json.loads(data))
            class R:
                status_code = 200
                text = "ok"
                def raise_for_status(self):
                    return None
            return R()

        def _fake_update_zenodo(deposition_id, paths, sandbox=True, access_token=None, publish=False):
            class R:
                def json(self):
                    return {"links": {"html": "https://sandbox.zenodo.org/deposit/123456"}}
            return R()

        # Create dynamic JSON that tries to include a DOI (should be ignored)
        (self.data_dir / "zenodo_dynamic.json").write_text(json.dumps({
            "title": "NEW TITLE (should be ignored)",
            "version": "v999",
            "doi": "10.9999/fake.doi",  # This should be removed before merging
            "prereserve_doi": {"doi": "10.9999/fake.doi", "recid": 999},  # This too
            "description": "New description",
        }), encoding="utf-8")

        # Mock Zenodo client
        mock_zenodo2 = type('MockZenodo', (), {
            'access_token': None,
            'update': lambda *args, **kwargs: _fake_update_zenodo(**kwargs)
        })()

        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch.object(self.zenodo_mod.requests, "get", _fake_get), \
             patch.object(self.zenodo_mod.requests, "put", _fake_put), \
             patch.object(self.zenodo_mod.requests, "delete", lambda *a, **k: type('R', (), {'status_code': 204})()), \
             patch.object(self.zenodo_mod, "Zenodo", return_value=mock_zenodo2), \
             patch.object(self.zenodo_mod, "_markdown_to_html", lambda s: "<p>Updated</p>"), \
             override_settings(
                 ZENODO_UPLOADS_ENABLED=True,
                 ZENODO_API_TOKEN="test_token",
                 ZENODO_API_BASE="https://sandbox.zenodo.org/api"
             ):

            call_command(
                "deposit_zenodo",
                "--deposition-id", "123456",
                "--token", "test_token",
            )

        # Verify captured metadata
        merged = captured_metadata.get("metadata", {})

        # DOI should be preserved from existing (not overwritten)
        self.assertEqual(merged.get("doi"), "10.5072/zenodo.123456",
                        "DOI should be preserved from existing deposition")
        self.assertNotEqual(merged.get("doi"), "10.9999/fake.doi",
                           "DOI should NOT be overwritten by incoming data")

        # prereserve_doi should also be preserved
        self.assertEqual(merged.get("prereserve_doi", {}).get("doi"), "10.5072/zenodo.123456",
                        "prereserve_doi should be preserved")

        # Non-DOI fields should be updated from incoming data (no longer protected)
        self.assertEqual(merged["title"], "NEW TITLE (should be ignored)",
                        "Title should be updated from incoming data")
        self.assertEqual(merged["upload_type"], "dataset",
                        "upload_type should be present")

        # Version and description should be updated
        self.assertEqual(merged["version"], "v999",
                        "Version should be updated (in default patch list)")
        self.assertIn("<p>Updated</p>", merged.get("description", ""),
                     "Description should be updated (in default patch list)")

    def test_grants_metadata_falls_back_to_notes_when_zenodo_rejects(self):
        """If Zenodo's curated grants vocabulary doesn't include a BMBF /
        BMFTR grant ID, the metadata PUT returns 400 — the deposit must
        retry once without `grants` and append a free-text funding
        statement to `metadata.notes` so the info isn't lost (issue #63
        Q2 decision)."""
        existing = {
            "submitted": False,
            "state": "unsubmitted",
            "links": {"edit": "http://edit", "bucket": "http://bucket"},
            "metadata": {
                "title": "T", "upload_type": "dataset",
                "publication_date": "2025-01-01",
                "creators": [{"name": "OPTIMAP"}],
                "version": "v1", "description": "<p>x</p>",
            },
        }

        (self.data_dir / "zenodo_dynamic.json").write_text(json.dumps({
            "title": "T", "version": "v2",
            "grants": [
                {"id": "10.13039/501100002347::16TOA028B"},
                {"id": "10.13039/501100002347::16KOA009A"},
            ],
        }), encoding="utf-8")

        puts: list[dict] = []

        def _fake_get(url, params=None, **kwargs):
            class R:
                status_code = 200; text = "ok"
                def json(self_): return deepcopy(existing)
                def raise_for_status(self_): return None
            return R()

        def _fake_put(url, params=None, data=None, headers=None, **kwargs):
            payload = json.loads(data) if data else {}
            puts.append(payload)
            class R:
                # First PUT: 400 because the grants list isn't curated.
                # Second PUT: 200 because the fallback removed `grants`.
                status_code = 400 if len(puts) == 1 else 200
                text = (
                    '{"errors":[{"field":"metadata.grants","message":"not found"}]}'
                    if len(puts) == 1 else "ok"
                )
                def raise_for_status(self_):
                    if self_.status_code >= 400:
                        import requests
                        raise requests.HTTPError(f"{self_.status_code} {self_.text}")
            return R()

        def _fake_update_zenodo(deposition_id, paths, sandbox=True, access_token=None, publish=False):
            class R:
                def json(self_):
                    return {"links": {"html": f"https://sandbox.zenodo.org/deposit/{deposition_id}"}}
            return R()

        mock_zenodo = type('MockZenodo', (), {
            'access_token': None,
            'update': lambda *a, **kw: _fake_update_zenodo(**kw),
        })()

        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch.object(self.zenodo_mod.requests, "get", _fake_get), \
             patch.object(self.zenodo_mod.requests, "put", _fake_put), \
             patch.object(self.zenodo_mod.requests, "delete",
                          lambda *a, **k: type('R', (), {'status_code': 204})()), \
             patch.object(self.zenodo_mod, "Zenodo", return_value=mock_zenodo), \
             patch.object(self.zenodo_mod, "_markdown_to_html", lambda s: "<p>x</p>"), \
             override_settings(
                 ZENODO_UPLOADS_ENABLED=True,
                 ZENODO_API_TOKEN="tok",
                 ZENODO_API_BASE="https://sandbox.zenodo.org/api",
             ):
            call_command("deposit_zenodo", "--deposition-id", "123456", "--token", "tok")

        # Two PUTs: one with grants (rejected), one without (succeeded)
        self.assertEqual(len(puts), 2)
        first, second = puts[0]["metadata"], puts[1]["metadata"]

        # First attempt sent both grant IDs
        self.assertEqual(
            [g["id"] for g in first.get("grants", [])],
            ["10.13039/501100002347::16TOA028B", "10.13039/501100002347::16KOA009A"],
        )
        # Fallback PUT carries no `grants`, but funding info lives in `notes`
        self.assertNotIn("grants", second)
        self.assertIn("OPTIMETA", second.get("notes", ""))
        self.assertIn("KOMET", second.get("notes", ""))
        self.assertIn("16TOA028B", second.get("notes", ""))
        self.assertIn("16KOA009A", second.get("notes", ""))


class DepositionIdResolutionTest(TestCase):
    """Resolution + bootstrap + new-version flow (issue #63 item 2)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.project_root = Path(self._tmpdir.name)
        self.templates_dir = self.project_root / "works" / "templates"
        self.data_dir = self.project_root / "data"
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        (self.data_dir / "README.md").write_text("# Title\n\nSome text.", encoding="utf-8")
        (self.data_dir / "optimap-main.zip").write_bytes(b"ZIP")
        (self.data_dir / "zenodo_dynamic.json").write_text(json.dumps({
            "title": "OPTIMAP FAIR Data Package",
            "version": "v1",
            "related_identifiers": [],
        }), encoding="utf-8")
        (self.data_dir / "optimap_data_dump_20250101.geojson").write_text("{}", encoding="utf-8")

        Work.objects.create(title="A", publicationDate="2010-10-10")

        import importlib
        self.zenodo_mod = importlib.import_module("works.zenodo")

        class FakePath(Path):
            _flavour = Path(".")._flavour
            def resolve(self):
                return self
        self.FakePath = FakePath
        self.zenodo_file = str(self.project_root / "works" / "zenodo.py")

    def tearDown(self):
        self._tmpdir.cleanup()

    def _draft_metadata(self):
        return {
            "submitted": False,
            "state": "unsubmitted",
            "links": {"edit": "http://edit"},
            "metadata": {
                "title": "OPTIMAP",
                "upload_type": "dataset",
                "publication_date": "2025-01-01",
                "creators": [{"name": "OPTIMAP"}],
                "version": "v0",
                "description": "<p>x</p>",
            },
        }

    def _patches(self, *, fake_get, fake_post, fake_put, mock_zenodo):
        return [
            patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file),
            patch.object(self.zenodo_mod, "Path", self.FakePath),
            patch.object(self.zenodo_mod.requests, "get", fake_get),
            patch.object(self.zenodo_mod.requests, "post", fake_post),
            patch.object(self.zenodo_mod.requests, "put", fake_put),
            patch.object(
                self.zenodo_mod.requests, "delete",
                lambda *a, **k: type("R", (), {"status_code": 204})(),
            ),
            patch.object(self.zenodo_mod, "Zenodo", return_value=mock_zenodo),
            patch.object(self.zenodo_mod, "_markdown_to_html", lambda s: "<p>x</p>"),
        ]

    def test_bootstrap_creates_new_draft_when_no_id_and_no_prior_log(self):
        """Issue #63 item 2: ``write code to create a new deposition``.
        With no env/setting ID and no successful log row, the deposit must
        POST /deposit/depositions to bootstrap a fresh draft, then use the
        returned id for the rest of the cycle."""
        from works.zenodo import deposit_to_zenodo

        posted_urls: list[str] = []

        def _fake_post(url, params=None, headers=None, data=None, **kwargs):
            posted_urls.append(url)
            class R:
                status_code = 201
                text = "ok"
                def json(self_): return {"id": 987654, "links": {"self": "http://x/987654"}}
                def raise_for_status(self_): return None
            return R()

        outer_self = self
        def _fake_get(url, params=None, **kwargs):
            class R:
                status_code = 200
                text = "ok"
                def json(self_): return deepcopy(outer_self._draft_metadata())
                def raise_for_status(self_): return None
            return R()

        def _fake_put(url, params=None, data=None, headers=None, **kwargs):
            class R:
                status_code = 200
                text = "ok"
                def raise_for_status(self_): return None
            return R()

        captured = {}
        def _fake_update(deposition_id, paths, sandbox=True, access_token=None, publish=False):
            captured["deposition_id"] = deposition_id
            class R:
                def json(self_): return {"links": {"html": f"https://sandbox.zenodo.org/deposit/{deposition_id}"}}
            return R()

        mock_zenodo = type("MockZenodo", (), {
            "access_token": None,
            "update": lambda *a, **kw: _fake_update(**kw),
        })()

        ctx = self._patches(
            fake_get=_fake_get, fake_post=_fake_post, fake_put=_fake_put,
            mock_zenodo=mock_zenodo,
        )
        from contextlib import ExitStack
        with ExitStack() as stack, override_settings(
            ZENODO_API_TOKEN="tok",
            ZENODO_API_BASE="https://sandbox.zenodo.org/api",
        ):
            for p in ctx:
                stack.enter_context(p)
            log_entry = deposit_to_zenodo()

        # POST to /deposit/depositions was made
        self.assertTrue(any(u.endswith("/deposit/depositions") for u in posted_urls),
                        f"Expected bootstrap POST, got: {posted_urls}")
        # The log row uses the bootstrapped ID
        self.assertEqual(log_entry.deposition_id, "987654")
        self.assertEqual(log_entry.status, "success")
        self.assertEqual(captured.get("deposition_id"), "987654")

    def test_resolves_from_latest_log_when_no_id_supplied(self):
        """When no explicit ID is set but a prior successful log exists for
        the same api_base, reuse that ID (no bootstrap POST)."""
        from works.zenodo import deposit_to_zenodo

        api_base = "https://sandbox.zenodo.org/api"
        ZenodoDepositionLog.objects.create(
            deposition_id="555555", api_base=api_base, status="success", version="v3",
        )

        outer = self
        def _fake_post(url, **kw):
            raise AssertionError(f"Bootstrap POST should not happen; got {url}")

        def _fake_get(url, params=None, **kwargs):
            class R:
                status_code = 200
                text = "ok"
                def json(self_): return deepcopy(outer._draft_metadata())
                def raise_for_status(self_): return None
            return R()

        def _fake_put(url, params=None, data=None, headers=None, **kwargs):
            class R:
                status_code = 200
                text = "ok"
                def raise_for_status(self_): return None
            return R()

        captured = {}
        def _fake_update(deposition_id, paths, sandbox=True, access_token=None, publish=False):
            captured["deposition_id"] = deposition_id
            class R:
                def json(self_): return {"links": {"html": "https://sandbox.zenodo.org/deposit/555555"}}
            return R()

        mock_zenodo = type("MockZenodo", (), {
            "access_token": None,
            "update": lambda *a, **kw: _fake_update(**kw),
        })()

        from contextlib import ExitStack
        with ExitStack() as stack, override_settings(
            ZENODO_API_TOKEN="tok", ZENODO_API_BASE=api_base,
        ):
            for p in self._patches(
                fake_get=_fake_get, fake_post=_fake_post,
                fake_put=_fake_put, mock_zenodo=mock_zenodo,
            ):
                stack.enter_context(p)
            log_entry = deposit_to_zenodo()

        self.assertEqual(log_entry.deposition_id, "555555")
        self.assertEqual(captured.get("deposition_id"), "555555")

    def test_new_version_when_target_is_already_published(self):
        """Once the previously deposited record has been manually published,
        the next run must POST .../actions/newversion and target the new
        draft instead — otherwise the PUT/upload would 400."""
        from works.zenodo import deposit_to_zenodo

        published = {
            "submitted": True,
            "state": "done",
            "links": {
                "edit": "http://edit",
                "self": "https://sandbox.zenodo.org/api/deposit/depositions/111",
            },
            "metadata": {
                "title": "OPTIMAP",
                "upload_type": "dataset",
                "publication_date": "2025-01-01",
                "creators": [{"name": "OPTIMAP"}],
                "version": "v1",
                "description": "<p>x</p>",
                "doi": "10.5281/zenodo.111",
            },
        }
        new_draft = {
            "submitted": False,
            "state": "unsubmitted",
            "links": {"edit": "http://edit"},
            "metadata": {
                "title": "OPTIMAP",
                "upload_type": "dataset",
                "publication_date": "2025-01-01",
                "creators": [{"name": "OPTIMAP"}],
                "version": "v1",
                "description": "<p>x</p>",
            },
        }

        gets: list[str] = []

        def _fake_get(url, params=None, **kwargs):
            gets.append(url)
            payload = published if "/depositions/111" in url else new_draft
            class R:
                status_code = 200
                text = "ok"
                def json(self_): return deepcopy(payload)
                def raise_for_status(self_): return None
            return R()

        posted: list[str] = []

        def _fake_post(url, params=None, headers=None, data=None, **kwargs):
            posted.append(url)
            class R:
                status_code = 201
                text = "ok"
                def json(self_):
                    # newversion response carries latest_draft pointing at the new ID
                    return {"links": {
                        "latest_draft": "https://sandbox.zenodo.org/api/deposit/depositions/222"
                    }}
                def raise_for_status(self_): return None
            return R()

        def _fake_put(url, params=None, data=None, headers=None, **kwargs):
            class R:
                status_code = 200
                text = "ok"
                def raise_for_status(self_): return None
            return R()

        captured = {}
        def _fake_update(deposition_id, paths, sandbox=True, access_token=None, publish=False):
            captured["deposition_id"] = deposition_id
            class R:
                def json(self_): return {"links": {"html": f"https://sandbox.zenodo.org/deposit/{deposition_id}"}}
            return R()

        mock_zenodo = type("MockZenodo", (), {
            "access_token": None,
            "update": lambda *a, **kw: _fake_update(**kw),
        })()

        from contextlib import ExitStack
        with ExitStack() as stack, override_settings(
            ZENODO_API_TOKEN="tok",
            ZENODO_API_BASE="https://sandbox.zenodo.org/api",
        ):
            for p in self._patches(
                fake_get=_fake_get, fake_post=_fake_post,
                fake_put=_fake_put, mock_zenodo=mock_zenodo,
            ):
                stack.enter_context(p)
            log_entry = deposit_to_zenodo(deposition_id="111")

        # The newversion POST landed on the published deposit
        self.assertTrue(
            any(u.endswith("/depositions/111/actions/newversion") for u in posted),
            f"Expected newversion POST; got: {posted}",
        )
        # The log row tracks the new draft ID, not the old published one
        self.assertEqual(log_entry.deposition_id, "222")
        self.assertEqual(captured.get("deposition_id"), "222")
        # And the upload+PUT targeted the new draft (verified via update call)


class ResolveHelpersTest(SimpleTestCase):
    """Sanity-check the URL/ID helpers in isolation."""

    def test_extract_id_from_url(self):
        from works.zenodo import _extract_id_from_url
        self.assertEqual(_extract_id_from_url(
            "https://sandbox.zenodo.org/api/deposit/depositions/12345"), "12345")
        self.assertEqual(_extract_id_from_url(
            "https://sandbox.zenodo.org/api/deposit/depositions/12345/"), "12345")
        self.assertIsNone(_extract_id_from_url(None))
        self.assertIsNone(_extract_id_from_url(""))

    def test_is_published_only_when_both_flags_match(self):
        from works.zenodo import _is_published
        self.assertTrue(_is_published({"submitted": True, "state": "done"}))
        self.assertFalse(_is_published({"submitted": False, "state": "done"}))
        self.assertFalse(_is_published({"submitted": True, "state": "inprogress"}))
        self.assertFalse(_is_published({"submitted": True, "state": "unsubmitted"}))
        self.assertFalse(_is_published({}))
