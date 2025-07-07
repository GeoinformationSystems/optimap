import os
from datetime import date
from zenodo_client import Zenodo, Metadata, Creator
from zenodo_client.struct import Community

class ZenodoService:
    """
    Encapsulates Zenodo sandbox/prod interactions: creating drafts, uploading files,
    updating metadata, and manual publication.
    """

    def __init__(self):
        # Determine sandbox or production
        use_sandbox = os.getenv('OPTIMAP_ZENODO_USE_SANDBOX', 'True') == 'True'
        token = os.getenv(
            'OPTIMAP_ZENODO_TOKEN_SANDBOX' if use_sandbox else 'OPTIMAP_ZENODO_TOKEN_PROD'
        )
        # Initialize client
        self.client = Zenodo(sandbox=use_sandbox)
        self.client.access_token = token

    def create_draft(self, key: str, metadata: Metadata, paths: list[str]) -> dict:
        """
        Create a new Zenodo deposition draft without publishing.

        :param key: unique key to identify deposition
        :param metadata: fully populated Metadata object
        :param paths: list of file paths to upload
        :returns: JSON response of the created draft
        """
        response = self.client.create(data=metadata, paths=paths, publish=False)
        return response.json()

    def update_draft(self, deposition_id: str, paths: list[str]) -> dict:
        """
        Update an existing draft: upload additional files or overwrite.

        :param deposition_id: ID of the draft to update
        :param paths: list of file paths to upload
        :returns: JSON response of the updated draft
        """
        response = self.client.update(deposition_id=deposition_id, paths=paths, publish=False)
        return response.json()

    def publish(self, deposition_id: str) -> dict:
        """
        Manually publish a previously created draft.

        :param deposition_id: ID of the draft to publish
        :returns: JSON response of the published record
        """
        response = self.client.publish(deposition_id=deposition_id)
        return response.json()

    @staticmethod
    def build_metadata(title: str,
                       upload_type: str,
                       description: str,
                       creators: list[Creator],
                       related_identifiers: list[dict],
                       communities: list[Community],
                       keywords: list[str],
                       notes: str) -> Metadata:
        """
        Helper to assemble a Metadata object with common fields.
        """
        m = Metadata(
            title=title,
            upload_type=upload_type,
            description=description,
            creators=creators,
            access_right='open',
            license='cc-by-4.0',
            related_identifiers=related_identifiers,
        )
        m.version = date.today().isoformat()
        m.publication_type = 'dataset'
        m.communities = communities
        m.keywords = keywords
        m.notes = notes
        return m
