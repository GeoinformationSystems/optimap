const dataCopyright = " | Publication metadata license: <a href='https://creativecommons.org/publicdomain/zero/1.0/'>CC-0</a>";
const publications_url = '/api/v1/publications.json?limit=999999';

async function initMap() {
  const map = L.map("map");

    var osmLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: 'Map data: &copy; <a href="https://openstreetmap.org">OpenStreetMap</a> contributors' + dataCopyright,
        maxZoom: 18
    }).addTo(map);

    //var esriWorldImageryLayer = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    //    attribution: 'Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community' + dataCopyright,
    //    maxZoom: 18
    //}).addTo(map);

    var baseLayers = {
        "OpenStreetMap": osmLayer,
        //"Esri World Imagery": esriWorldImageryLayer
    };

  const publicationsGroup = new L.FeatureGroup();
  map.addLayer(publicationsGroup);

  const overlayMaps = {
    Publications: publicationsGroup,
  };

  L.control.scale({ position: "bottomright" }).addTo(map);
  L.control.layers(baseLayers, overlayMaps).addTo(map);

  const publications = await load_publications();
  const publicationsLayer = L.geoJSON(publications, {
    onEachFeature: publicationPopup,
  });
  publicationsLayer.eachLayer(function (l) {
    publicationsGroup.addLayer(l);
  });

  if (publicationsGroup.getBounds().isValid()) {
    map.fitBounds(publicationsGroup.getBounds());
  }
}

function publicationPopup(feature, layer) {
  let popupContent = "<div>";

  // 1. Title
  if (feature.properties.title) {
    popupContent += "<h3>" + feature.properties.title + "</h3>";
  }

  // 2. Source details (nested Journal info)
  if (feature.properties.source_details) {
    const j = feature.properties.source_details;

    // 2a. Journal Name linked to OpenAlex
    const journalName = j.name || "Unknown Journal";
    const openalexUrl = j.openalex_id
      ? j.openalex_id
      : j.issn_l
      ? `https://openalex.org/sources/issn:${j.issn_l}`
      : null;
    const journalLink = openalexUrl
      ? `<a href="${openalexUrl}" target="_blank">${journalName}</a>`
      : `<span>${journalName}</span>`;
    popupContent += `<div><strong>Journal:</strong> ${journalLink}</div>`;

    // 2b. ISSN (hyperlinked)
    if (j.issn_l) {
      const issnLink = `<a href="https://openalex.org/sources/issn:${j.issn_l}" target="_blank">${j.issn_l}</a>`;
      popupContent += `<div><strong>ISSN:</strong> ${issnLink}</div>`;
    }

    // 2c. Publisher Name
    if (j.publisher_name) {
      popupContent += `<div><strong>Publisher:</strong> ${j.publisher_name}</div>`;
    }
  }

  // 3. Timeperiod
  if (
    feature.properties.timeperiod_startdate &&
    feature.properties.timeperiod_enddate
  ) {
    popupContent +=
      "<div><b>Timeperiod:</b> from " +
      feature.properties.timeperiod_startdate +
      " to " +
      feature.properties.timeperiod_enddate +
      "</div>";
  }

  // 4. Abstract
  if (feature.properties.abstract) {
    popupContent += "<div><p>" + feature.properties.abstract + "</p></div>";
  }

  // 5. Article URL
  if (feature.properties.url) {
    popupContent +=
      '<div><a href="' +
      feature.properties.url +
      '" target="_blank">Visit Article</a></div>';
  }

  popupContent += "</div>";

  layer.bindPopup(popupContent, {
    maxWidth: 250,
    maxHeight: 225,
  });
}

async function load_publications() {
  const response = await fetch(publications_url);
  const body = await response.json();
  console.log("OPTIMAP retrieved " + body.count + " results.");
  return body.results;
}

document.addEventListener("DOMContentLoaded", initMap);
