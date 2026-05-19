"""Management command wrapper for deposit_to_zenodo()."""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from works.zenodo import deposit_to_zenodo


class Command(BaseCommand):
    help = "Update an existing Zenodo deposition draft with generated files and selectively patched metadata."

    def add_arguments(self, parser):
        parser.add_argument("--deposition-id", dest="deposition_id", help="Existing deposition (draft) ID on Zenodo.")
        parser.add_argument(
            "--patch",
            dest="patch",
            default=(
                "description,version,keywords,related_identifiers,"
                "additional_descriptions,grants,title,upload_type,"
                "publication_date,creators"
            ),
            help="Comma-separated list of metadata fields to patch (others are preserved).",
        )
        parser.add_argument("--merge-keywords", action="store_true", help="Merge incoming keywords with existing.")
        parser.add_argument("--merge-related", action="store_true", help="Merge incoming related_identifiers.")
        parser.add_argument("--no-build", action="store_true", help="(Kept for compatibility; ignored here.)")
        parser.add_argument("--token", dest="token", help="Zenodo API token (overrides env/settings).")

    def handle(self, *args, **opts):
        # Resolve deposition ID — optional. When unset, deposit_to_zenodo()
        # falls back to the latest successful log row for this api_base, and
        # if there is none, bootstraps a fresh draft via POST /deposit/depositions.
        deposition_id = opts.get("deposition_id") or os.getenv("ZENODO_SANDBOX_DEPOSITION_ID") or getattr(
            settings, "ZENODO_SANDBOX_DEPOSITION_ID", None
        )

        # Resolve API base
        api_base = os.getenv("ZENODO_API_BASE") or getattr(settings, "ZENODO_API_BASE", "https://sandbox.zenodo.org/api")

        self.stdout.write(f"Depositing OPTIMAP data dump to {api_base} (configured via settings/default)")
        if deposition_id:
            self.stdout.write(f"Using deposition ID {deposition_id}")
        else:
            self.stdout.write("No deposition ID configured — will reuse the latest from the log or bootstrap a new draft.")

        try:
            log_entry = deposit_to_zenodo(
                deposition_id=str(deposition_id) if deposition_id else None,
                api_base=api_base,
                token=opts.get("token"),
                patch_fields=opts.get("patch"),
                merge_keywords=opts.get("merge_keywords", False),
                merge_related=opts.get("merge_related", False),
                stdout_callback=self.stdout.write,
            )

            if log_entry.status == 'success':
                self.stdout.write(self.style.SUCCESS("✓ Deposit completed successfully"))
                if log_entry.zenodo_url:
                    self.stdout.write(f"\nNote: This deposition is in DRAFT state and not yet published.")
                    self.stdout.write(f"Review at: {log_entry.zenodo_url}")
            else:
                raise CommandError(f"Deposition failed: {log_entry.error_message}")

        except Exception as ex:
            raise CommandError(f"Deposition failed: {ex}") from ex
