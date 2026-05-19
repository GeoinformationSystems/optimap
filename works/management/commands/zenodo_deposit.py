"""
Management command to trigger a complete Zenodo deposition cycle.

This command runs both render_zenodo and deposit_zenodo in sequence,
making it easy to manually trigger a full deposition to Zenodo.

Usage:
    python manage.py zenodo_deposit
    python manage.py zenodo_deposit --deposition-id 123456
    python manage.py zenodo_deposit --token YOUR_TOKEN
"""
import os
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command


class Command(BaseCommand):
    help = "Trigger a complete Zenodo deposition cycle (render + deposit)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--deposition-id",
            dest="deposition_id",
            help="Existing deposition (draft) ID on Zenodo. Uses ZENODO_SANDBOX_DEPOSITION_ID if not provided.",
        )
        parser.add_argument(
            "--token",
            dest="token",
            help="Zenodo API token (overrides env/settings).",
        )
        parser.add_argument(
            "--skip-render",
            action="store_true",
            help="Skip the render step and only run deposit (assumes files already exist).",
        )
        parser.add_argument(
            "--patch",
            dest="patch",
            default="description,version,keywords,related_identifiers",
            help="Comma-separated list of metadata fields to patch (default: description,version,keywords,related_identifiers).",
        )
        parser.add_argument(
            "--merge-keywords",
            action="store_true",
            help="Merge incoming keywords with existing (don't replace).",
        )
        parser.add_argument(
            "--merge-related",
            action="store_true",
            help="Merge incoming related_identifiers with existing (don't replace).",
        )

    def handle(self, *args, **opts):
        deposition_id = opts.get("deposition_id") or os.getenv("ZENODO_SANDBOX_DEPOSITION_ID")
        token = opts.get("token")

        api_base = os.getenv("ZENODO_API_BASE") or getattr(
            settings, "ZENODO_API_BASE", "https://sandbox.zenodo.org/api"
        )

        self.stdout.write(self.style.SUCCESS("\n" + "="*70))
        self.stdout.write(self.style.SUCCESS("  Zenodo Deposition Manager"))
        self.stdout.write(self.style.SUCCESS("="*70))
        self.stdout.write(f"\nTarget: {api_base}")
        if deposition_id:
            self.stdout.write(f"Deposition ID: {deposition_id}\n")
        else:
            self.stdout.write(
                "Deposition ID: (none configured — will reuse latest from log or bootstrap a new draft)\n"
            )

        # Step 1: Render (unless skipped)
        if not opts.get("skip_render"):
            self.stdout.write(self.style.WARNING("\n[Step 1/2] Rendering data files and metadata..."))
            try:
                call_command("render_zenodo", stdout=self.stdout, stderr=self.stderr)
                self.stdout.write(self.style.SUCCESS("✓ Render completed successfully\n"))
            except Exception as ex:
                self.stdout.write(self.style.ERROR(f"✗ Render failed: {ex}"))
                raise CommandError(f"Render step failed: {ex}") from ex
        else:
            self.stdout.write(self.style.WARNING("\n[Step 1/2] Skipping render step (--skip-render)\n"))

        # Step 2: Deposit
        self.stdout.write(self.style.WARNING("[Step 2/2] Uploading to Zenodo..."))
        try:
            deposit_opts = {
                "patch": opts.get("patch"),
                "merge_keywords": opts.get("merge_keywords", False),
                "merge_related": opts.get("merge_related", False),
            }
            if deposition_id:
                deposit_opts["deposition_id"] = deposition_id
            if token:
                deposit_opts["token"] = token

            call_command("deposit_zenodo", **deposit_opts, stdout=self.stdout, stderr=self.stderr)
            self.stdout.write(self.style.SUCCESS("✓ Deposit completed successfully\n"))
        except Exception as ex:
            self.stdout.write(self.style.ERROR(f"✗ Deposit failed: {ex}"))
            raise CommandError(f"Deposit step failed: {ex}") from ex

        # Summary
        self.stdout.write(self.style.SUCCESS("\n" + "="*70))
        self.stdout.write(self.style.SUCCESS("  Zenodo deposition completed successfully!"))
        self.stdout.write(self.style.SUCCESS("="*70))
        self.stdout.write("\nNext steps:")
        if deposition_id:
            self.stdout.write("  • Check the deposition at: " + api_base.replace("/api", f"/deposit/{deposition_id}"))
        else:
            self.stdout.write("  • Check the admin → Zenodo Deposition Logs for the new draft URL")
        self.stdout.write("  • Review files and metadata")
        self.stdout.write("  • Publish when ready (cannot be undone!)")
        self.stdout.write(self.style.WARNING("\nNote: This deposition is in DRAFT state and not yet published.\n"))
