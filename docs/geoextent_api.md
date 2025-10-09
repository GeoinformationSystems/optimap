# Geoextent API

## Overview

A implementation of REST API endpoints that expose the functionality of the [geoextent library](https://github.com/nuest/geoextent/) for extracting geospatial and temporal extents from various file formats and remote repositories.
Response formats are close to the geoextent library output, with additional structured formats for easier consumption of WKT and WKB outputs.

## Endpoints

1. **`/api/v1/geoextent/extract/`** (POST)
   - File upload via multipart/form-data
   - File size validation
   - Temporary file handling with cleanup
   - Optional placename lookup

1. **`/api/v1/geoextent/extract-remote/`** (POST, GET)
   - Remote repository extraction (Zenodo, PANGAEA, etc.) via multipart/form-data or via URL parameter
   - Download workers configuration
   - Size and file limits
   - Optional placename lookup

1. **`/api/v1/geoextent/extract-batch/`** (POST)
   - Multiple file upload
   - Total size validation
   - Per-file error handling
   - Combined extent calculation
   - Optional placename for combined result


## Parameter Summary

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `response_format` | string | `geojson` | Output format: `geojson`, `wkt`, or `wkb` |
| `bbox` | boolean | `true` | Extract spatial bounding box |
| `tbox` | boolean | `true` | Extract temporal extent |
| `convex_hull` | boolean | `false` | Use convex hull instead of bounding box |

## Property Names

The API uses the same property names as the geoextent CLI tool to avoid confusion:

- `tbox`: Temporal extent (not `temporal_extent`)
- `geoextent_extraction`: Top-level metadata object
- `inputs`: Input identifiers (not `identifiers_processed`)
- `files_processed`, `files_with_extent`, `total_size`: Statistics fields

## HTTP Status Codes

- `200 OK`: Successful extraction
- `400 Bad Request`: Invalid parameters
- `413 Request Entity Too Large`: File too large
- `500 Internal Server Error`: Processing error

Error responses contain only an `error` field with the error message (no `success: false` property).

## Usage examples

1. **Single file extraction**:

   ```bash
   curl -X POST http://127.0.0.1:8000/api/v1/geoextent/extract/ \
     -F "file=@test.geojson" \
     -F "bbox=true" \
     -F "tbox=true" \
     -F "placename=true" \
     -F "gazetteer=nominatim"
   ```

1. **Remote extraction**:

   ```bash
   curl -X POST http://127.0.0.1:8000/api/v1/geoextent/extract-remote/ \
     -H "Content-Type: application/json" \
     -d '{
       "identifier": "10.5281/zenodo.4593540",
       "bbox": true,
       "tbox": true,
       "placename": true,
       "file_limit": 5
     }'
   ```

1- **Multiple identifiers with GeoJSON format**:

   ```bash
   curl -X POST http://localhost:8000/api/v1/geoextent/extract-remote/ \
   -H "Content-Type: application/json" \
   -d '{
      "identifiers": ["10.5281/zenodo.4593540", "10.5281/zenodo.1234567"],
      "bbox": true,
      "tbox": true,
      "response_format": "geojson",
   }'
   ```

1. **Batch extraction**:

   ```bash
   curl -X POST http://127.0.0.1:8000/api/v1/geoextent/extract-batch/ \
     -F "files=@file1.geojson" \
     -F "files=@file2.tif" \
     -F "bbox=true" \
     -F "combine_extents=true" \
     -F "placename=true"
   ```

**OpenAPI docs**:

<http://127.0.0.1:8000/api/schema/ui/>

## Available Formats

Switchable via `response_format` parameter.

### `geojson`

Returns spatial extent as a GeoJSON FeatureCollection with temporal data and metadata in properties.

**Example:**

```bash
curl -X POST http://localhost:8000/api/v1/geoextent/extract-remote/ \
  -H "Content-Type: application/json" \
  -d '{"identifiers": ["10.5281/zenodo.4593540"], "bbox": true, "tbox": true}'
```

Or via GET:

```bash
curl "http://localhost:8000/api/v1/geoextent/extract-remote/?identifiers=10.5281/zenodo.4593540&bbox=true&tbox=true"
```

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {
        "type": "Polygon",
        "coordinates": [
          [
            [39.642802545572735, -80.71456319678893],
            [42.256308231814586, -80.71456319678893],
            [42.256308231814586, -74.78657735361809],
            [39.642802545572735, -74.78657735361809],
            [39.642802545572735, -80.71456319678893]
          ]
        ]
      },
      "properties": {
        "tbox": ["2006-02-02", "2018-08-27"]
      }
    }
  ],
  "geoextent_extraction": {
    "version": "0.9.1.dev3+g42ab7cff2.d20251006",
    "inputs": ["10.5281/zenodo.4593540"],
    "statistics": {
      "files_processed": 1,
      "files_with_extent": 1,
      "total_size": "2.71 MiB"
    },
    "format": "remote",
    "crs": "4326",
    "extent_type": "bounding_box"
  }
}
```

#### GeoJSON Structure

- `type`: Always "FeatureCollection"
- `features`: Array of GeoJSON Feature objects
  - `geometry`: Polygon geometry representing the spatial extent
  - `properties.tbox`: Temporal extent (if requested with `tbox=true`)
- `geoextent_extraction`: Extraction metadata
  - `version`: Geoextent library version
  - `inputs`: List of input files/identifiers
  - `statistics`: Files processed, files with extent, total size
  - `format`: Source format (e.g., "remote", "geojson", "geotiff")
  - `crs`: Coordinate reference system
  - `extent_type`: "bounding_box" or "convex_hull"

### `wkt`

Returns spatial extent as Well-Known Text (WKT) string with CRS information.

**Example:**

```bash
curl -X POST http://localhost:8000/api/v1/geoextent/extract-remote/ \
  -H "Content-Type: application/json" \
  -d '{"identifiers": ["10.5281/zenodo.4593540"], "bbox": true, "tbox": true, "response_format": "wkt"}'
```

```json
{
  "wkt": "POLYGON ((39.642802545572735 -80.71456319678893, 39.642802545572735 -74.78657735361809, 42.256308231814586 -74.78657735361809, 42.256308231814586 -80.71456319678893, 39.642802545572735 -80.71456319678893))",
  "crs": "EPSG:4326",
  "tbox": ["2006-02-02", "2018-08-27"],
  "geoextent_extraction": {
    "version": "0.9.1.dev3+g42ab7cff2.d20251006",
    "inputs": ["10.5281/zenodo.4593540"],
    "format": "remote",
    "crs": "4326",
    "extent_type": "bounding_box"
  }
}
```

### `wkb`

Returns spatial extent as Well-Known Binary (WKB) hex string with CRS information.

**Example:**

```bash
curl -X POST http://localhost:8000/api/v1/geoextent/extract-remote/ \
  -H "Content-Type: application/json" \
  -d '{"identifiers": ["10.5281/zenodo.4593540"], "bbox": true, "tbox": true, "response_format": "wkb"}'
```

```json
{
  "wkb": "0103000000010000000500000054e3a59bc4f2434054e3a59bc4f2434054e3a59bc4f2434054e3a59bc4f2434054e3a59bc4f24340",
  "crs": "EPSG:4326",
  "tbox": ["2006-02-02", "2018-08-27"],
  "geoextent_extraction": {
    "version": "0.9.1.dev3+g42ab7cff2.d20251006",
    "inputs": ["10.5281/zenodo.4593540"],
    "format": "remote",
    "crs": "4326",
    "extent_type": "bounding_box"
  }
}
```

## Error Handling

If a spatial extent cannot be converted to the requested format (e.g., no bbox available), the API returns a JSON error message with HTTP status code 400:

```json
{
  "success": false,
  "error": "Cannot convert to geojson: no spatial extent available"
}
```

## Configuration Examples

### Development Configuration

In `.env`:

```env
OPTIMAP_DEBUG=True
OPTIMAP_GEOEXTENT_MAX_FILE_SIZE_MB=50
OPTIMAP_GEOEXTENT_TIMEOUT=60
OPTIMAP_GEOEXTENT_DOWNLOAD_WORKERS=2
```

### Production Configuration

In `.env`:

```env
OPTIMAP_DEBUG=False
OPTIMAP_GEOEXTENT_MAX_FILE_SIZE_MB=100
OPTIMAP_GEOEXTENT_MAX_DOWNLOAD_SIZE_MB=500
OPTIMAP_GEOEXTENT_MAX_BATCH_SIZE_MB=250
OPTIMAP_GEOEXTENT_TIMEOUT=30
OPTIMAP_GEOEXTENT_DOWNLOAD_WORKERS=4
OPTIMAP_GEOEXTENT_GEONAMES_USERNAME=your_username_here
```

## References

- **Geoextent Library**: <https://github.com/nuest/geoextent/>
- **Geoextent Documentation**: <https://nuest.github.io/geoextent/>
