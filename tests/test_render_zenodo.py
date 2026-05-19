# tests/test_render_zenodo.py
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from works.models import Work, Source, ZenodoDepositionLog


class RenderZenodoTest(TestCase):
    def setUp(self):
        # Temp “project root”
        self._tmpdir = tempfile.TemporaryDirectory()
        self.project_root = Path(self._tmpdir.name)
        self.templates_dir = self.project_root / "works" / "templates"
        self.cmds_dir = self.project_root / "works" / "management" / "commands"
        self.data_dir = self.project_root / "data"
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        self.cmds_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Copy the real README.md.j2 from the source tree so the codebook /
        # cross-format prose are the same in tests as in production. This
        # keeps assertions on README content honest.
        real_template = (
            Path(__file__).resolve().parents[1] / "works" / "templates" / "README.md.j2"
        )
        (self.templates_dir / "README.md.j2").write_text(
            real_template.read_text(encoding="utf-8"), encoding="utf-8",
        )

        # DB fixtures
        Work.objects.create(title="A", publicationDate="2010-10-10")

        # Bad labels to clean
        Source.objects.create(name="2000", url_field="https://optimap.science")  # numeric-only -> OPTIMAP
        Source.objects.create(name="",     url_field="https://example.org")      # blank -> domain label
        Source.objects.create(name=" ",    url_field="https://example.org")      # duplicate -> dedupe

        # Good label
        Source.objects.create(
            name="AGILE: GIScience Series",
            url_field="https://agile-giss.copernicus.org"
        )

        # Import zenodo module after DB is ready
        import importlib
        self.zenodo_mod = importlib.import_module("works.zenodo")

        # Fake Path so resolve() stays inside tmp root
        class FakePath(Path):
            _flavour = Path(".")._flavour
            def resolve(self):
                return self
        self.FakePath = FakePath
        self.zenodo_file = str(self.project_root / "works" / "zenodo.py")

    def tearDown(self):
        self._tmpdir.cleanup()

    def _fake_git_archive(self, *args, **kwargs):
        """Stand-in for subprocess.run([git archive…]) that writes a small
        non-empty zip at the path given via the `-o` argument, so the render
        step's hard failure-on-empty check stays satisfied."""
        argv = args[0] if args else kwargs.get("args", [])
        if "-o" in argv:
            out_path = Path(argv[argv.index("-o") + 1])
            out_path.write_bytes(b"PK\x03\x04stub")
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    def test_render_produces_clean_readme_and_assets(self):
        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch("subprocess.run", self._fake_git_archive):
            call_command("render_zenodo")

        readme_path = self.data_dir / "README.md"
        zip_path    = self.data_dir / "optimap-main.zip"
        dyn_path    = self.data_dir / "zenodo_dynamic.json"

        self.assertTrue(readme_path.exists(), "README.md not generated")
        self.assertTrue(zip_path.exists(), "optimap-main.zip not generated")
        self.assertTrue(dyn_path.exists(), "zenodo_dynamic.json not generated")

        md = readme_path.read_text(encoding="utf-8")
        # Sources cleanup assertions
        self.assertNotIn("- [2000](", md, "Numeric-only label leaked into Sources")
        self.assertIn("- [OPTIMAP](https://optimap.science)", md, "OPTIMAP override missing")
        self.assertIn("AGILE: GIScience Series", md, "Named source missing")
        # example.org should appear only once after dedupe
        self.assertEqual(md.count("example.org"), 1, "Duplicate source/domain not deduped")

    @override_settings(BASE_URL="https://optimap.science")
    def test_render_includes_live_download_urls_as_related_identifiers(self):
        """Each render must overwrite related_identifiers with the live
        download URLs derived from settings.BASE_URL — never trust a stale
        zenodo_dynamic.json (issue #63, item 5)."""
        # Seed a stale dyn file with a localhost identifier; render must drop it.
        (self.data_dir / "zenodo_dynamic.json").write_text(json.dumps({
            "related_identifiers": [
                {"scheme": "url", "identifier": "http://127.0.0.1:8000/stale",
                 "relation": "isSupplementTo", "resource_type": "dataset"}
            ]
        }), encoding="utf-8")

        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch("subprocess.run", self._fake_git_archive):
            call_command("render_zenodo")

        dyn = json.loads((self.data_dir / "zenodo_dynamic.json").read_text(encoding="utf-8"))
        live_urls = {
            r["identifier"]
            for r in dyn["related_identifiers"]
            if r["relation"] == "isSupplementTo"
        }
        self.assertEqual(live_urls, {
            "https://optimap.science/download/geojson/",
            "https://optimap.science/download/geopackage/",
            "https://optimap.science/download/csv/",
        })
        for r in dyn["related_identifiers"]:
            if r["relation"] == "isSupplementTo":
                self.assertEqual(r["resource_type"], "dataset")
                self.assertEqual(r["scheme"], "url")

    @override_settings(BASE_URL="https://optimap.science")
    def test_render_includes_describes_entry_per_source(self):
        """Each Source becomes one related_identifiers entry with
        relation=describes. ISSN-L wins over URL; sources sharing a
        canonical identifier are deduped; optimap.science is skipped
        (issue #63, item 6 / comment 2025-07-14)."""
        # Source with an ISSN-L → scheme=issn
        Source.objects.create(
            name="Earth System Science Data",
            url_field="https://essd.copernicus.org/oai",
            homepage_url="https://www.earth-system-science-data.net/",
            issn_l="1866-3508",
        )
        # Source without ISSN-L but with homepage → scheme=url, identifier=homepage
        Source.objects.create(
            name="Some Repository",
            url_field="https://example.org/oai",
            homepage_url="https://example.com/journal",
        )

        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch("subprocess.run", self._fake_git_archive):
            call_command("render_zenodo")

        dyn = json.loads((self.data_dir / "zenodo_dynamic.json").read_text(encoding="utf-8"))
        describes = [
            r for r in dyn["related_identifiers"] if r["relation"] == "describes"
        ]
        for r in describes:
            self.assertEqual(r["resource_type"], "publication")

        idents = {(r["scheme"], r["identifier"]) for r in describes}

        # ISSN-L wins over homepage URL
        self.assertIn(("issn", "1866-3508"), idents)
        # Homepage URL is the fallback (canonicalised to https + lowercased host)
        self.assertIn(("url", "https://example.com/journal"), idents)
        # optimap.science (seeded in setUp via numeric-name source) must not
        # appear — the portal isn't a source it describes.
        for scheme, ident in idents:
            self.assertNotIn("optimap.science", ident)
        # Two sources point at example.org and example.com but the dedupe key
        # is the resolved identifier, so they coexist; the duplicate
        # example.org seed in setUp has no homepage_url so falls back to its
        # url_field once after dedupe.
        self.assertEqual(
            sum(1 for s, i in idents if "example.org" in i), 1,
            "Duplicate example.org Sources should collapse to one describes entry",
        )

    def test_render_raises_when_git_archive_fails(self):
        """A failed `git archive` must propagate so the deposit doesn't ship
        an empty optimap-main.zip (issue #63, last checklist item)."""
        import subprocess

        def _failing(*a, **k):
            raise subprocess.CalledProcessError(
                returncode=128, cmd=a[0] if a else [], stderr="fatal: not a git repository"
            )

        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch("subprocess.run", _failing):
            with self.assertRaisesRegex(Exception, r"git archive HEAD.*failed"):
                call_command("render_zenodo")

    def test_render_default_keywords_match_issue_decisions(self):
        """Keywords default to the list agreed in nuest's 2025-07-14 comment.
        Both `Open Research Information` and its short form `ORI` ship so
        the record is findable under either label."""
        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch("subprocess.run", self._fake_git_archive):
            call_command("render_zenodo")

        dyn = json.loads((self.data_dir / "zenodo_dynamic.json").read_text(encoding="utf-8"))
        self.assertEqual(dyn["keywords"], [
            "Open Access", "Open Science", "Open Research Information",
            "ORI", "Open Data", "FAIR",
        ])

    def test_render_version_starts_at_v1_with_no_prior_deposits(self):
        """Fresh DB, no ZenodoDepositionLog rows → render emits v1.
        The data/last_version.txt file was removed in favour of DB state."""
        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch("subprocess.run", self._fake_git_archive):
            call_command("render_zenodo")

        dyn = json.loads((self.data_dir / "zenodo_dynamic.json").read_text(encoding="utf-8"))
        self.assertEqual(dyn["version"], "v1")
        # And the legacy tracking file must not be created either.
        self.assertFalse((self.data_dir / "last_version.txt").exists())

    def test_render_version_increments_from_latest_successful_log(self):
        """Render reads the latest successful ZenodoDepositionLog for the
        target api_base and emits the next vN. Sandbox and production
        increment independently; failed depositions don't burn a version."""
        api_base = "https://sandbox.zenodo.org/api"
        # Successful logs at v1 and v2 for this api_base; the latest wins.
        ZenodoDepositionLog.objects.create(
            deposition_id="42", api_base=api_base, status="success", version="v1",
        )
        ZenodoDepositionLog.objects.create(
            deposition_id="42", api_base=api_base, status="success", version="v2",
        )
        # A failed deposit at v3 must not advance the counter.
        ZenodoDepositionLog.objects.create(
            deposition_id="42", api_base=api_base, status="failed", version="v3",
        )
        # A successful deposit at a different api_base must not advance it either.
        ZenodoDepositionLog.objects.create(
            deposition_id="99", api_base="https://zenodo.org/api",
            status="success", version="v50",
        )

        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch("subprocess.run", self._fake_git_archive), \
             override_settings(ZENODO_API_BASE=api_base):
            call_command("render_zenodo")

        dyn = json.loads((self.data_dir / "zenodo_dynamic.json").read_text(encoding="utf-8"))
        self.assertEqual(dyn["version"], "v3")

    def test_render_emits_grants_for_optimeta_and_komet(self):
        """Render emits structured `grants` for OPTIMETA (BMBF 16TOA028B)
        and KOMET (BMFTR 16KOA009A), per the 2025-08-21 issue comment on
        #63 (NFDI4Earth intentionally excluded)."""
        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch("subprocess.run", self._fake_git_archive):
            call_command("render_zenodo")

        dyn = json.loads((self.data_dir / "zenodo_dynamic.json").read_text(encoding="utf-8"))
        grant_ids = [g["id"] for g in dyn.get("grants", [])]
        self.assertEqual(grant_ids, [
            "10.13039/501100002347::16TOA028B",  # OPTIMETA
            "10.13039/501100002347::16KOA009A",  # KOMET
        ])
        # Only `id` keys are exposed to Zenodo — the human-readable
        # name/funder/grant labels live in the _FUNDING constant.
        for g in dyn["grants"]:
            self.assertEqual(list(g.keys()), ["id"])

    def test_render_emits_license_split_additional_description(self):
        """License split (CC0 for data, GPL-3.0 for code) is documented as a
        Zenodo `additional_descriptions` entry of type=notes — per the
        2025-07-21 issue comment."""
        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch("subprocess.run", self._fake_git_archive):
            call_command("render_zenodo")

        dyn = json.loads((self.data_dir / "zenodo_dynamic.json").read_text(encoding="utf-8"))
        notes = dyn.get("additional_descriptions") or []
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["type"], "notes")
        html = notes[0]["description"]
        # Both licenses called out, with their actual file scopes
        self.assertIn("CC0-1.0", html)
        self.assertIn("GPL-3.0", html)
        self.assertIn("optimap-main.zip", html)
        self.assertIn("optimap_data_dump_*.csv", html)
        self.assertIn("optimap_data_dump_*.gpkg", html)

    def test_render_codebook_covers_post_rebase_fields(self):
        """README codebook mentions the fields added since the initial
        Zenodo branch (type, authors, keywords, topics, bok_concepts,
        placename, country_code, openalex_id) and notes cross-format
        equivalence (WKT in CSV)."""
        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch("subprocess.run", self._fake_git_archive):
            call_command("render_zenodo")

        md = (self.data_dir / "README.md").read_text(encoding="utf-8")
        # Cross-format note
        self.assertIn("CSV column", md)
        self.assertIn("WKT", md)
        # New fields
        for field in (
            "`type`", "`authors`", "`keywords`", "`topics`",
            "`bok_concepts`", "`placename`", "`country_code`",
            "`openalex_id`",
        ):
            self.assertIn(field, md, f"codebook is missing {field}")

    def test_render_raises_when_git_archive_writes_empty_file(self):
        """If `git archive` exits 0 but writes a 0-byte file (corrupt repo,
        SIGPIPE, …) we still fail rather than uploading an empty zip."""
        def _empty_archive(*args, **kwargs):
            argv = args[0] if args else kwargs.get("args", [])
            if "-o" in argv:
                out_path = Path(argv[argv.index("-o") + 1])
                out_path.write_bytes(b"")
            class _R:
                returncode = 0
                stderr = "warning: empty tree"
            return _R()

        with patch.object(self.zenodo_mod, "__file__", new=self.zenodo_file), \
             patch.object(self.zenodo_mod, "Path", self.FakePath), \
             patch("subprocess.run", _empty_archive):
            with self.assertRaisesRegex(Exception, r"produced no archive"):
                call_command("render_zenodo")
