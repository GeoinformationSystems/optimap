#!/bin/bash
# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

# Script to download all external JavaScript and CSS libraries
# Run this script from the publications/static/ directory

set -e  # Exit on error

echo "Downloading JavaScript libraries..."

# Create directories if they don't exist
mkdir -p js css/images css/fonts webfonts img

echo "  - ORCID iD icon (16px)"
wget -q "https://orcid.org/sites/default/files/images/orcid_16x16.png" -O img/orcid-id-icon-16.png

# Core libraries (already present, but updated versions)
echo "  - jQuery 3.4.1"
wget -q https://code.jquery.com/jquery-3.4.1.slim.min.js -O js/jquery-3.4.1.slim.min.js

echo "  - Bootstrap 4.4.1 JS"
wget -q https://cdn.jsdelivr.net/npm/bootstrap@4.4.1/dist/js/bootstrap.min.js -O js/bootstrap.min.js
wget -q https://cdn.jsdelivr.net/npm/bootstrap@4.4.1/dist/js/bootstrap.min.js.map -O js/bootstrap.min.js.map

echo "  - Popper.js 1.16.1 (required by Bootstrap 4 tooltips/popovers/dropdowns)"
# Bootstrap 4 calls `new Popper(...)` and is ONLY compatible with Popper.js v1.x
# (the `popper.js` package). Popper v2 (`@popperjs/core`) removed the constructor
# and throws "Popper is not a constructor" when a tooltip is shown. Do not upgrade
# this to @popperjs/core@2 unless Bootstrap itself is upgraded to v5.
wget -q https://unpkg.com/popper.js@1.16.1/dist/umd/popper.min.js -O js/popper.min.js

# Leaflet core (upgraded to 1.9.4)
echo "  - Leaflet 1.9.4"
wget -q https://unpkg.com/leaflet@1.9.4/dist/leaflet.js -O js/leaflet.js
wget -q https://unpkg.com/leaflet@1.9.4/dist/leaflet.js.map -O js/leaflet.js.map

# Leaflet plugins
echo "  - Leaflet Draw 1.0.4"
wget -q https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js -O js/leaflet.draw.js

echo "  - Leaflet Fullscreen 3.0.2"
wget -q https://unpkg.com/leaflet.fullscreen@3.0.2/Control.FullScreen.js -O js/leaflet.fullscreen.js

echo "  - Leaflet Control Geocoder 2.4.0"
wget -q https://unpkg.com/leaflet-control-geocoder@2.4.0/dist/Control.Geocoder.js -O js/leaflet.control.geocoder.js

# Bootstrap Datepicker
echo "  - Bootstrap Datepicker 1.9.0"
wget -q https://cdnjs.cloudflare.com/ajax/libs/bootstrap-datepicker/1.9.0/js/bootstrap-datepicker.min.js -O js/bootstrap-datepicker.min.js

# MapLibre GL JS (used for vector tile basemaps, e.g. basemap.world)
echo "  - MapLibre GL JS 2.4.0"
wget -q https://unpkg.com/maplibre-gl@2.4.0/dist/maplibre-gl.js -O js/maplibre-gl.js

echo "  - MapLibre GL Leaflet 0.0.17"
wget -q https://unpkg.com/@maplibre/maplibre-gl-leaflet@0.0.17/leaflet-maplibre-gl.js -O js/leaflet-maplibre-gl.js

echo ""
echo "Downloading CSS libraries..."

# Bootstrap CSS
echo "  - Bootstrap 4.4.1 CSS"
wget -q https://cdn.jsdelivr.net/npm/bootstrap@4.4.1/dist/css/bootstrap.min.css -O css/bootstrap.min.css
wget -q https://cdn.jsdelivr.net/npm/bootstrap@4.4.1/dist/css/bootstrap.min.css.map -O css/bootstrap.min.css.map

# Leaflet CSS (upgraded to 1.9.4)
echo "  - Leaflet 1.9.4 CSS"
wget -q https://unpkg.com/leaflet@1.9.4/dist/leaflet.css -O css/leaflet.css

# Leaflet images
echo "  - Leaflet marker images"
wget -q https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png -O css/images/marker-icon.png
wget -q https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png -O css/images/marker-icon-2x.png
wget -q https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png -O css/images/marker-shadow.png
wget -q https://unpkg.com/leaflet@1.9.4/dist/images/layers.png -O css/images/layers.png
wget -q https://unpkg.com/leaflet@1.9.4/dist/images/layers-2x.png -O css/images/layers-2x.png

# Leaflet Draw CSS
echo "  - Leaflet Draw 1.0.4 CSS"
wget -q https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css -O css/leaflet.draw.css

# Leaflet Draw images
echo "  - Leaflet Draw images"
wget -q https://unpkg.com/leaflet-draw@1.0.4/dist/images/spritesheet.png -O css/images/spritesheet.png
wget -q https://unpkg.com/leaflet-draw@1.0.4/dist/images/spritesheet-2x.png -O css/images/spritesheet-2x.png
wget -q https://unpkg.com/leaflet-draw@1.0.4/dist/images/spritesheet.svg -O css/images/spritesheet.svg

# Leaflet Fullscreen CSS
echo "  - Leaflet Fullscreen 3.0.2 CSS"
wget -q https://unpkg.com/leaflet.fullscreen@3.0.2/Control.FullScreen.css -O css/leaflet.fullscreen.css

# Leaflet Control Geocoder CSS
echo "  - Leaflet Control Geocoder 2.4.0 CSS"
wget -q https://unpkg.com/leaflet-control-geocoder@2.4.0/dist/Control.Geocoder.css -O css/leaflet.control.geocoder.css

# Leaflet Fullscreen images
echo "  - Leaflet Fullscreen images"
mkdir -p css/images/fullscreen
wget -q https://unpkg.com/leaflet.fullscreen@3.0.2/icon-fullscreen.png -O css/images/fullscreen/icon-fullscreen.png
wget -q https://unpkg.com/leaflet.fullscreen@3.0.2/icon-fullscreen-2x.png -O css/images/fullscreen/icon-fullscreen-2x.png

# Leaflet Control Geocoder images
echo "  - Leaflet Control Geocoder images"
wget -q https://unpkg.com/leaflet-control-geocoder@2.4.0/dist/images/geocoder.png -O css/images/geocoder.png 2>/dev/null || true
wget -q https://unpkg.com/leaflet-control-geocoder@2.4.0/dist/images/throbber.gif -O css/images/throbber.gif 2>/dev/null || true

# Bootstrap Datepicker CSS
echo "  - Bootstrap Datepicker 1.9.0 CSS"
wget -q https://cdnjs.cloudflare.com/ajax/libs/bootstrap-datepicker/1.9.0/css/bootstrap-datepicker.min.css -O css/bootstrap-datepicker.min.css

# MapLibre GL CSS
echo "  - MapLibre GL JS 2.4.0 CSS"
wget -q https://unpkg.com/maplibre-gl@2.4.0/dist/maplibre-gl.css -O css/maplibre-gl.css

# Font Awesome 4.7.0 (minimal version for backward compatibility)
echo "  - Font Awesome 4.7.0 CSS"
wget -q https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/css/font-awesome.min.css -O css/font-awesome.min.css

echo "  - Font Awesome 4.7.0 fonts"
wget -q https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/fonts/fontawesome-webfont.eot -O css/fonts/fontawesome-webfont.eot
wget -q https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/fonts/fontawesome-webfont.svg -O css/fonts/fontawesome-webfont.svg
wget -q https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/fonts/fontawesome-webfont.ttf -O css/fonts/fontawesome-webfont.ttf
wget -q https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/fonts/fontawesome-webfont.woff -O css/fonts/fontawesome-webfont.woff
wget -q https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/fonts/fontawesome-webfont.woff2 -O css/fonts/fontawesome-webfont.woff2
wget -q https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/fonts/FontAwesome.otf -O css/fonts/FontAwesome.otf

echo ""
echo "All libraries downloaded successfully!"
echo ""
echo "Files are located in:"
echo "  - JavaScript: publications/static/js/"
echo "  - CSS: publications/static/css/"
echo "  - Images: publications/static/css/images/"
echo "  - Fonts: publications/static/css/fonts/"
