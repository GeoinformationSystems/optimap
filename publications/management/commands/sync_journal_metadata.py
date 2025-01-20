import logging
import requests
from django.core.management.base import BaseCommand
from publications.models import Publication

# Configure logging
logger = logging.getLogger(__name__)

class OpenAlexClient:
    BASE_URL = "https://api.openalex.org"

    def fetch_journal_by_issn(self, issn):
        """Fetch journal metadata from OpenAlex by ISSN."""
        url = f"{self.BASE_URL}/sources/issn:{issn}"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching journal data for ISSN {issn}: {e}")
            return None

    def fetch_journal_by_id(self, openalex_id):
        """Fetch journal metadata from OpenAlex by OpenAlex ID."""
        url = f"{self.BASE_URL}/sources/{openalex_id}"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching journal data for OpenAlex ID {openalex_id}: {e}")
            return None


class Command(BaseCommand):
    help = "Sync journal metadata using the OpenAlex API."

    def handle(self, *args, **options):
        logger.info("Starting journal metadata synchronization...")

        # Initialize OpenAlex client
        client = OpenAlexClient()

        # Fetch all publications
        publications = Publication.objects.all()
        for publication in publications:
            journal_name = publication.journal_name
            journal_identifier = publication.journal_identifier

            if journal_identifier:
                logger.info(f"Fetching metadata for ISSN {journal_identifier}...")
                metadata = client.fetch_journal_by_issn(journal_identifier)

                if metadata:
                    self.update_publication_metadata(publication, metadata)
                else:
                    logger.warning(f"Failed to fetch metadata for ISSN {journal_identifier}.")
            elif journal_name:
                logger.info(f"No ISSN for {journal_name}. Processing as-is.")
                # No ISSN - fallback for handling incomplete metadata
            else:
                logger.warning(f"Publication {publication.id} has no journal name or identifier.")

        logger.info("Journal metadata synchronization complete.")

    def update_publication_metadata(self, publication, metadata):
        """Update publication with fetched journal metadata."""
        try:
            publication.journal_name = metadata.get("display_name", publication.journal_name)
            publication.journal_identifier = metadata.get("issn_l", publication.journal_identifier)
            publication.publisher = metadata.get("host_organization_name")
            publication.openalex_url = metadata.get("id")
            publication.save()
            logger.info(f"Updated metadata for publication {publication.id}.")
        except Exception as e:
            logger.error(f"Error updating metadata for publication {publication.id}: {e}")
