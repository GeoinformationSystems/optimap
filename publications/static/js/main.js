// publications/static/js/main.js

// Leaflet map initialization and popup rendering for publication points

// 1. Load all publications from the API
async function load_publications() {
  const response = await fetch(publications_url);
  const body = await response.json();
  console.log(`OPTIMAP retrieved ${body.count} results.`);
  return body.results;
}

// 2. Once the DOM is ready, initialize the map
$(document).ready(function() {
  initMap();
});

// API URL and copyright attribution
const publications_url = '/api/v1/publications/?limit=999999';
const dataCopyright =
  " | Publication metadata license: <a href='https://creativecommons.org/publicdomain/zero/1.0/'>CC-0</a>";

async function initMap() {
  const map = L.map('map');

  // Base layer: OpenStreetMap
  const osmLayer = L.tileLayer(
    'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    {
      attribution:
        'Map data: © <a href="https://openstreetmap.org">OpenStreetMap</a> contributors' +
        dataCopyright,
      maxZoom: 18,
    }
  ).addTo(map);

  // Group to hold all publication markers
  const publicationsGroup = new L.FeatureGroup().addTo(map);

  // Controls: scale and layer switcher
  L.control.scale({ position: 'bottomright' }).addTo(map);
  L.control
    .layers(
      { 'OpenStreetMap': osmLayer },
      { Publications: publicationsGroup }
    )
    .addTo(map);

  // Fetch data and add to map
  const pubs = await load_publications();
  const pubsLayer = L.geoJSON(pubs, {
    style: feature => ({
     color: feature.properties.source_details.is_preprint ? 'orange' : 'blue',
     weight: 3,
     fillOpacity: 0.2,
   }),
    onEachFeature: publicationPopup
  });
  pubsLayer.eachLayer((layer) => publicationsGroup.addLayer(layer));

  // Fit map to markers
  if (publicationsGroup.getBounds().isValid()) {
    map.fitBounds(publicationsGroup.getBounds());
  }
}

// 3. Popup content generator for each publication feature
function publicationPopup(feature, layer) {
  const p = feature.properties;
  let html = '<div>';

  // Title
  if (p.title) html += `<h3>${p.title}</h3>`;

  // Source details from nested object
  if (p.source_details) {
    const s = p.source_details;

    // Display name
    const name = s.display_name || s.name || 'Unknown';
    html += `<div><strong>Source:</strong> ${name}</div>`;

    // Abbreviated title
    if (s.abbreviated_title) {
      html += `<div><em>${s.abbreviated_title}</em></div>`;
    }

    // Homepage link
    if (s.homepage_url) {
      html += `<div><a href="${s.homepage_url}" target="_blank">Visit journal site</a></div>`;
    }

    // ISSN-L link
    if (s.issn_l) {
      html +=
        `<div><strong>ISSN-L:</strong> ` +
        `<a href="https://openalex.org/sources/issn:${s.issn_l}" target="_blank">${s.issn_l}</a></div>`;
    }

    // Publisher (only if different from display name)
    if (s.publisher_name && s.publisher_name !== name) {
      html += `<div><strong>Publisher:</strong> ${s.publisher_name}</div>`;
    }

    // Open access status
    if ('is_oa' in s) {
      const status = s.is_oa ? 'Open Access' : 'Closed Access';
      html += `<div><strong>Access:</strong> ${status}</div>`;
    }

    // Citation count
    if (s.cited_by_count != null) {
      html += `<div>Cited by ${s.cited_by_count} works</div>`;
    }

    // Works count
    if (s.works_count != null) {
      html += `<div>${s.works_count} works hosted</div>`;
    }
  }

  // Time period
  if (p.timeperiod_startdate && p.timeperiod_enddate) {
    html +=
      `<div><strong>Timeperiod:</strong> from ${p.timeperiod_startdate} to ${p.timeperiod_enddate}</div>`;
  }

  // Abstract
  if (p.abstract) html += `<div><p>${p.abstract}</p></div>`;

  // Article link
  if (p.url) {
    html += `<div><a href="${p.url}" target="_blank">Visit Article</a></div>`;
  }

  html += '</div>';
  layer.bindPopup(html, { maxWidth: 300, maxHeight: 250 });
}
