# Provenance of dependencies

## JS file sources

```bash
wget https://code.jquery.com/jquery-3.4.1.min.js -P js/
wget https://cdn.jsdelivr.net/npm/bootstrap@4.4.1/dist/js/bootstrap.min.js -P js/
wget https://cdn.jsdelivr.net/npm/bootstrap@4.4.1/dist/js/bootstrap.min.js.map -P js/
wget https://unpkg.com/leaflet@1.9.2/dist/leaflet.js -P js/
wget https://unpkg.com/leaflet@1.9.2/dist/leaflet.js.map -P js/
```

Required for Bootstrap tooltips in REST Framework UI:

```bash
wget https://unpkg.com/@popperjs/core@2 -O js/popper.js
```

## CSS file sources

```bash
wget https://cdn.jsdelivr.net/npm/bootstrap@4.4.1/dist/css/bootstrap.min.css -P css/
wget https://cdn.jsdelivr.net/npm/bootstrap@4.4.1/dist/css/bootstrap.min.css.map -P css/
wget https://unpkg.com/leaflet@1.9.2/dist/leaflet.css -P css/

wget https://unpkg.com/leaflet@1.9.2/dist/images/marker-icon.png -P css/images
wget https://unpkg.com/leaflet@1.9.2/dist/images/marker-icon-2x.png -P css/images
wget https://unpkg.com/leaflet@1.9.2/dist/images/marker-shadow.png -P css/images
wget https://unpkg.com/leaflet@1.9.2/dist/images/layers.png -P css/images
wget https://unpkg.com/leaflet@1.9.2/dist/images/layers-2x.png -P css/images
```

## Fonts

<https://use.fontawesome.com/releases/v6.2.1/fontawesome-free-6.2.1-web.zip>

See <https://fontawesome.com/docs/web/setup/host-yourself/webfonts>.
We only use the (free) 'solid' variant of CSS and fonts.
