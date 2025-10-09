"""publications API views."""

import json
import logging
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from rest_framework import viewsets, status
from rest_framework_gis import filters
from rest_framework.permissions import IsAuthenticatedOrReadOnly, AllowAny
from rest_framework.decorators import action
from rest_framework.response import Response
from django.conf import settings
from django.contrib.gis.geos import Polygon, Point, MultiPoint

# Import geoextent at module level
import geoextent.lib.extent as geoextent

from .models import Publication, Source, Subscription
from .serializers import (
    PublicationSerializer,
    SourceSerializer,
    SubscriptionSerializer,
    GeoextentExtractSerializer,
    GeoextentRemoteSerializer,
    GeoextentRemoteGetSerializer,
    GeoextentBatchSerializer,
)

logger = logging.getLogger(__name__)

class SourceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Source.objects.all()
    serializer_class = SourceSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

class PublicationViewSet(viewsets.ReadOnlyModelViewSet):
    bbox_filter_field = "geometry"
    filter_backends = (filters.InBBoxFilter,)
    serializer_class = PublicationSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    queryset = Publication.objects.filter(status="p").distinct()

class SubscriptionViewSet(viewsets.ModelViewSet):
    """
    Subscription view set.
    Each user can list, create, update, or delete their own Subscriptions.
    """
    bbox_filter_field = "region"
    filter_backends = (filters.InBBoxFilter,)
    serializer_class = SubscriptionSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        return Subscription.objects.filter(user=user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class GeoextentViewSet(viewsets.ViewSet):
    """
    ViewSet for extracting geospatial and temporal extents from files.

    Provides three endpoints:
    - extract: Extract from uploaded file
    - extract-remote: Extract from remote repository (Zenodo, PANGAEA, etc.)
    - extract-batch: Extract from multiple uploaded files

    Public API - no authentication required.
    """
    permission_classes = [AllowAny]

    def _cleanup_temp_file(self, filepath):
        """Delete temporary file safely."""
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
                logger.debug(f"Cleaned up temp file: {filepath}")
        except Exception as e:
            logger.warning(f"Failed to cleanup temp file {filepath}: {e}")

    def _save_uploaded_file(self, uploaded_file):
        """Save uploaded file to temporary location."""
        temp_dir = Path(settings.GEOEXTENT_TEMP_DIR)
        temp_dir.mkdir(exist_ok=True, parents=True)

        # Generate unique filename
        file_ext = Path(uploaded_file.name).suffix
        temp_filename = f"{uuid.uuid4()}{file_ext}"
        temp_path = temp_dir / temp_filename

        # Save file
        with open(temp_path, 'wb+') as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)

        logger.info(f"Saved uploaded file to: {temp_path}")
        return str(temp_path)

    def _process_geoextent_result(self, result):
        """
        Process geoextent result and format for API response.
        Geoextent returns the extent information directly.
        """
        try:
            # Check if result is None or empty
            if result is None:
                logger.error("Geoextent returned None - no valid spatial data found")
                return None

            if not isinstance(result, dict):
                logger.error(f"Geoextent returned unexpected type: {type(result)}")
                return None

            response = {
                'success': True,
            }

            # Add spatial extent if present
            if 'bbox' in result:
                response['spatial_extent'] = result['bbox']

            # Add temporal extent if present
            if 'tbox' in result:
                response['temporal_extent'] = result['tbox']

            # Add placename if present (geoextent extracts this)
            if 'placename' in result and result['placename']:
                response['placename'] = result['placename']

            # Add external metadata if present (from CrossRef/DataCite)
            if 'external_metadata' in result and result['external_metadata']:
                response['external_metadata'] = result['external_metadata']

            # Add metadata
            response['metadata'] = {}
            if 'format' in result:
                response['metadata']['file_format'] = result['format']
            if 'crs' in result:
                response['metadata']['crs'] = result['crs']
            if 'file_size_bytes' in result:
                response['metadata']['file_size_bytes'] = result['file_size_bytes']

            return response
        except Exception as e:
            logger.error(f"Error processing geoextent result: {e}")
            raise

    def _build_geoextent_extraction_metadata(self, geoextent_result, identifiers=None):
        """
        Build geoextent_extraction metadata object matching CLI output format.

        Directly copies geoextent output structure to avoid confusion between API and CLI.
        """
        import geoextent

        metadata = {
            'version': geoextent.__version__,
            'inputs': identifiers if identifiers else [],
        }

        # Directly copy statistics from extraction_metadata if available
        if 'extraction_metadata' in geoextent_result:
            em = geoextent_result['extraction_metadata']
            stats = {}
            # Copy exactly as geoextent CLI returns them
            if 'total_resources' in em:
                stats['files_processed'] = em['total_resources']
            if 'successful_resources' in em:
                stats['files_with_extent'] = em['successful_resources']
            if 'total_size' in em:
                stats['total_size'] = em['total_size']
            if stats:
                metadata['statistics'] = stats

        # Directly copy format and CRS from geoextent result
        if 'format' in geoextent_result:
            metadata['format'] = geoextent_result['format']
        if 'crs' in geoextent_result:
            metadata['crs'] = geoextent_result['crs']

        # Determine extent type
        if geoextent_result.get('convex_hull'):
            metadata['extent_type'] = 'convex_hull'
        else:
            metadata['extent_type'] = 'bounding_box'

        return metadata

    def _format_response(self, geoextent_result, structured_result, response_format, identifiers=None):
        """
        Format the response based on the requested format.

        Args:
            geoextent_result: Raw result from geoextent (dict with bbox, tbox, etc.)
            structured_result: Processed structured result from _process_geoextent_result
            response_format: One of 'geojson', 'wkt', 'wkb'
            identifiers: List of input identifiers (for metadata)

        Returns:
            Formatted response based on response_format
        """
        if response_format == 'geojson':
            # Use geoextent's format_extent_output to create proper GeoJSON
            # This ensures we match CLI output exactly and don't need to manually
            # reconstruct GeoJSON from bbox
            import geoextent.lib.helpfunctions as hf

            # Build extraction metadata for geoextent's formatter
            extraction_metadata = self._build_geoextent_extraction_metadata(
                geoextent_result,
                identifiers=identifiers
            )

            # Use geoextent's official formatter to create GeoJSON FeatureCollection
            # This handles bbox, convex_hull, tbox, placename, external_metadata automatically
            formatted_output = hf.format_extent_output(
                geoextent_result,
                output_format='geojson',
                extraction_metadata=extraction_metadata
            )

            return formatted_output

        elif response_format in ['wkt', 'wkb']:
            # For WKT/WKB, we need to convert bbox to geometry
            if not structured_result.get('spatial_extent'):
                return {
                    'success': False,
                    'error': f'Cannot convert to {response_format}: no spatial extent available'
                }

            bbox = structured_result['spatial_extent']

            # Handle convex hull format (list of points)
            if isinstance(bbox, list) and len(bbox) > 0 and isinstance(bbox[0], list):
                # Convex hull: list of [lon, lat] points
                if len(bbox) == 1:
                    # Single point
                    geom = Point(bbox[0][0], bbox[0][1], srid=4326)
                else:
                    # Multiple points - create polygon from points
                    points = [(point[0], point[1]) for point in bbox]
                    # Close the polygon if not already closed
                    if points[0] != points[-1]:
                        points.append(points[0])
                    geom = Polygon(points, srid=4326)

            # Handle standard bbox format [min_lon, min_lat, max_lon, max_lat]
            elif isinstance(bbox, list) and len(bbox) == 4:
                geom = Polygon.from_bbox(bbox)
                geom.srid = 4326
            else:
                return {
                    'success': False,
                    'error': f'Cannot convert bbox format {bbox} to {response_format}'
                }

            # Build geoextent_extraction metadata
            geoextent_extraction = self._build_geoextent_extraction_metadata(
                geoextent_result,
                identifiers=identifiers
            )

            # Create result with geometry in requested format
            if response_format == 'wkt':
                result = {'wkt': geom.wkt}
            else:  # wkb
                result = {'wkb': geom.wkb.hex()}

            # Add common fields
            result['crs'] = 'EPSG:4326'
            result['geoextent_extraction'] = geoextent_extraction

            # Add tbox if present (using same property name as CLI)
            if structured_result.get('temporal_extent'):
                result['tbox'] = structured_result['temporal_extent']
            if structured_result.get('placename'):
                result['placename'] = structured_result['placename']
            if structured_result.get('external_metadata'):
                result['external_metadata'] = structured_result['external_metadata']

            return result

        # Default fallback
        return structured_result

    @action(detail=False, methods=['post'])
    def extract(self, request):
        """
        Extract geospatial and temporal extent from uploaded file.

        POST /api/v1/geoextent/extract/
        """
        serializer = GeoextentExtractSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        uploaded_file = serializer.validated_data['file']
        bbox = serializer.validated_data['bbox']
        tbox = serializer.validated_data['tbox']
        convex_hull = serializer.validated_data['convex_hull']
        response_format = serializer.validated_data['response_format']
        placename = serializer.validated_data['placename']
        gazetteer = serializer.validated_data['gazetteer']

        temp_path = None

        try:
            # Check file size
            max_size_bytes = settings.GEOEXTENT_MAX_FILE_SIZE_MB * 1024 * 1024
            if uploaded_file.size > max_size_bytes:
                return Response(
                    {
                        'success': False,
                        'error': 'File too large',
                        'details': f'File size ({uploaded_file.size} bytes) exceeds maximum ({max_size_bytes} bytes)'
                    },
                    status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                )

            # Save uploaded file
            temp_path = self._save_uploaded_file(uploaded_file)

            # Check if the file is a ZIP archive
            is_zip = zipfile.is_zipfile(temp_path)
            temp_dir = None

            if is_zip:
                # Extract ZIP to temporary directory and process with fromDirectory
                temp_dir = tempfile.mkdtemp(prefix='geoextent_zip_')
                logger.info(f"Extracting ZIP file to: {temp_dir}")

                with zipfile.ZipFile(temp_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)

                # Call geoextent.fromDirectory on extracted contents
                geoextent_result = geoextent.fromDirectory(
                    temp_dir,
                    bbox=bbox,
                    tbox=tbox,
                    convex_hull=convex_hull,
                    placename=gazetteer if placename else None,
                    show_progress=False,  # Disable progress bar in API
                    recursive=True,  # Process subdirectories in ZIP
                )
            else:
                # Call geoextent once with all parameters
                # placename parameter: None, 'nominatim', 'geonames', or 'photon'
                geoextent_result = geoextent.fromFile(
                    temp_path,
                    bbox=bbox,
                    tbox=tbox,
                    convex_hull=convex_hull,
                    placename=gazetteer if placename else None,
                    show_progress=False,  # Disable progress bar in API
                )

            # Process result to structured format
            structured_result = self._process_geoextent_result(geoextent_result)

            # Check if processing failed
            if structured_result is None:
                return Response({
                    'error': f'Could not extract spatial extent from "{uploaded_file.name}". The file may not contain valid spatial data or may be in an unsupported format.'
                }, status=status.HTTP_400_BAD_REQUEST)

            structured_result['filename'] = uploaded_file.name

            # Format response based on requested format
            result = self._format_response(
                geoextent_result,
                structured_result,
                response_format,
                identifiers=[uploaded_file.name]
            )

            return Response(result, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error processing file extraction: {e}", exc_info=True)
            return Response(
                {
                    'success': False,
                    'error': 'Processing error',
                    'details': str(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        finally:
            # Cleanup temp file
            if temp_path:
                self._cleanup_temp_file(temp_path)
            # Cleanup temp directory if ZIP was extracted
            if 'temp_dir' in locals() and temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"Cleaned up temp directory: {temp_dir}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp directory {temp_dir}: {e}")

    @action(detail=False, methods=['get', 'post'], url_path='extract-remote')
    def extract_remote(self, request):
        """
        Extract geospatial and temporal extent from one or more remote repositories.

        POST /api/v1/geoextent/extract-remote/ - JSON body with identifiers array
        GET /api/v1/geoextent/extract-remote/?identifiers=doi1,doi2 - URL parameters with comma-separated identifiers
        """
        # Use different serializers for GET vs POST
        if request.method == 'GET':
            serializer = GeoextentRemoteGetSerializer(data=request.query_params)
        else:
            serializer = GeoextentRemoteSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        identifiers = serializer.validated_data['identifiers']
        bbox = serializer.validated_data['bbox']
        tbox = serializer.validated_data['tbox']
        convex_hull = serializer.validated_data['convex_hull']
        response_format = serializer.validated_data['response_format']
        placename = serializer.validated_data['placename']
        gazetteer = serializer.validated_data['gazetteer']
        file_limit = serializer.validated_data['file_limit']
        size_limit_mb = serializer.validated_data['size_limit_mb']
        external_metadata = serializer.validated_data['external_metadata']
        external_metadata_method = serializer.validated_data['external_metadata_method']

        try:
            workers = settings.GEOEXTENT_DOWNLOAD_WORKERS

            # Pass identifiers as list or string to geoextent.fromRemote
            # It will handle combining extents natively
            geoextent_input = identifiers[0] if len(identifiers) == 1 else identifiers

            # Call geoextent once with all identifiers
            geoextent_result = geoextent.fromRemote(
                geoextent_input,
                bbox=bbox,
                tbox=tbox,
                convex_hull=convex_hull,
                details=True,  # Get individual results
                placename=gazetteer if placename else None,
                max_download_workers=workers,
                max_download_size=f"{size_limit_mb}MB" if size_limit_mb else None,
                show_progress=False,  # Disable progress bar in API
                download_skip_nogeo=True,  # Skip non-geospatial files
                ext_metadata=external_metadata,
                ext_metadata_method=external_metadata_method,
            )

            # For single identifier, geoextent returns simple format
            if len(identifiers) == 1:
                structured_result = self._process_geoextent_result(geoextent_result)

                # Check if processing failed
                if structured_result is None:
                    return Response({
                        'error': f'Could not extract spatial extent from "{identifiers[0]}". The resource may not contain valid spatial data or may be inaccessible.'
                    }, status=status.HTTP_400_BAD_REQUEST)

                structured_result['identifier'] = identifiers[0]
                formatted_result = self._format_response(
                    geoextent_result,
                    structured_result,
                    response_format,
                    identifiers=identifiers
                )
                return Response(formatted_result, status=status.HTTP_200_OK)

            # For multiple identifiers, geoextent returns remote_bulk format
            # Extract individual results from details
            individual_results = []
            if 'details' in geoextent_result:
                for identifier, file_result in geoextent_result['details'].items():
                    # Check if this result has an error
                    if 'error' in file_result:
                        individual_results.append({
                            'identifier': identifier,
                            'success': False,
                            'error': file_result['error']
                        })
                        continue

                    structured_result = self._process_geoextent_result(file_result)
                    structured_result['identifier'] = identifier

                    # Format based on response_format
                    formatted_result = self._format_response(
                        file_result,
                        structured_result,
                        response_format,
                        identifiers=[identifier]
                    )
                    if response_format not in ['geojson', 'wkt', 'wkb']:
                        formatted_result['identifier'] = identifier

                    individual_results.append(formatted_result)

            # Build response with combined extent (geoextent always combines)
            combined_structured = self._process_geoextent_result(geoextent_result)
            combined_formatted = self._format_response(
                geoextent_result,
                combined_structured,
                response_format,
                identifiers=identifiers
            )

            # For multiple identifiers, return structured response with combined + individual
            # For GeoJSON format, return FeatureCollection with all features
            if response_format == 'geojson':
                # Merge all features into single FeatureCollection
                all_features = []
                if isinstance(combined_formatted, dict) and 'features' in combined_formatted:
                    all_features = combined_formatted['features'].copy()

                # Add individual features
                for result in individual_results:
                    if isinstance(result, dict) and 'features' in result:
                        all_features.extend(result['features'])

                response_data = {
                    'type': 'FeatureCollection',
                    'features': all_features,
                    'geoextent_extraction': combined_formatted.get('geoextent_extraction', {})
                }
            else:
                # For WKT/WKB, return combined with metadata
                response_data = combined_formatted

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error processing remote extraction: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['post'], url_path='extract-batch')
    def extract_batch(self, request):
        """
        Extract geospatial and temporal extent from multiple uploaded files.

        POST /api/v1/geoextent/extract-batch/
        """
        serializer = GeoextentBatchSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        bbox = serializer.validated_data['bbox']
        tbox = serializer.validated_data['tbox']
        convex_hull = serializer.validated_data['convex_hull']
        response_format = serializer.validated_data['response_format']
        placename = serializer.validated_data['placename']
        gazetteer = serializer.validated_data['gazetteer']
        size_limit_mb = serializer.validated_data['size_limit_mb']

        # Get uploaded files from request
        files = request.FILES.getlist('files')
        if not files:
            return Response(
                {
                    'success': False,
                    'error': 'No files provided',
                    'details': 'At least one file must be uploaded'
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        temp_dir = None

        try:
            # Check total size
            total_size = sum(f.size for f in files)
            max_size_bytes = size_limit_mb * 1024 * 1024
            if total_size > max_size_bytes:
                return Response(
                    {
                        'success': False,
                        'error': 'Total size exceeds limit',
                        'details': f'Total size ({total_size} bytes) exceeds limit ({max_size_bytes} bytes)'
                    },
                    status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                )

            # Create a temporary directory for all uploaded files
            temp_dir = tempfile.mkdtemp(prefix='geoextent_batch_')
            logger.info(f"Created temp directory for batch processing: {temp_dir}")

            # Save all files to the temporary directory
            for uploaded_file in files:
                temp_path = os.path.join(temp_dir, uploaded_file.name)
                with open(temp_path, 'wb') as destination:
                    for chunk in uploaded_file.chunks():
                        destination.write(chunk)
                logger.debug(f"Saved {uploaded_file.name} to {temp_path}")

            # Use geoextent.fromDirectory to process all files at once
            # details=True provides individual file results
            geoextent_result = geoextent.fromDirectory(
                temp_dir,
                bbox=bbox,
                tbox=tbox,
                convex_hull=convex_hull,
                details=True,  # Get individual file details
                placename=gazetteer if placename else None,
                show_progress=False,  # Disable progress bar in API
                recursive=False,  # Don't traverse subdirectories
            )

            # Process combined result
            combined_structured = self._process_geoextent_result(geoextent_result)

            # Check if processing failed for combined result
            if combined_structured is None:
                filenames = ', '.join([f.name for f in files])
                return Response({
                    'success': False,
                    'error': f'Could not extract spatial extent from the uploaded files: {filenames}',
                    'details': 'The files may not contain valid spatial data or may be in unsupported formats.'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Process individual file results from details
            individual_results = []
            if 'details' in geoextent_result:
                for filename, file_result in geoextent_result['details'].items():
                    structured_result = self._process_geoextent_result(file_result)

                    # Skip files that failed processing
                    if structured_result is None:
                        logger.warning(f"Could not extract extent from {filename}")
                        individual_results.append({
                            'filename': filename,
                            'error': 'Could not extract spatial extent',
                            'details': 'The file may not contain valid spatial data or may be in an unsupported format.'
                        })
                        continue

                    structured_result['filename'] = filename

                    # Format based on response_format
                    formatted_result = self._format_response(
                        file_result,
                        structured_result,
                        response_format,
                        identifiers=[filename]
                    )
                    if response_format not in ['geojson', 'wkt', 'wkb']:
                        formatted_result['filename'] = filename

                    individual_results.append(formatted_result)

            # Build response with combined extent (geoextent always combines)
            filenames = [f.name for f in files]
            combined_formatted = self._format_response(
                geoextent_result,
                combined_structured,
                response_format,
                identifiers=filenames
            )

            response_data = {
                'success': True,
                'files_processed': len(files),
                'combined_extent': combined_formatted,
                'individual_results': individual_results
            }

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error processing batch extraction: {e}", exc_info=True)
            return Response(
                {
                    'success': False,
                    'error': 'Processing error',
                    'details': str(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        finally:
            # Cleanup temporary directory and all files
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"Cleaned up temp directory: {temp_dir}")
                except Exception as e:
                    logger.error(f"Error cleaning up temp directory {temp_dir}: {e}")