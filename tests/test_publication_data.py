import json
from django.test import TestCase
from django.core.serializers import serialize
from publications.models import Publication

EXTENDED_TEST_DATA = [
    {
        "status": "p",
        "title": "First Africa Publication",
        "abstract": "Sample publication covering a cluster of ca. 5–6 major cities in West Africa.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/africa1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POLYGON ((3.0000 4.5000, 9.0000 4.5000, 9.0000 12.5000, 3.0000 12.5000, 3.0000 4.5000)))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "Africa Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for Africa multi-city test."
    },
    {
        "status": "p",
        "title": "First Asia Publication",
        "abstract": "Sample publication covering a cluster of ca. 5–6 major cities in the Beijing area.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/asia1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POLYGON ((116.0000 39.0000, 118.0000 39.0000, 118.0000 41.0000, 116.0000 41.0000, 116.0000 39.0000)))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "Asia Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for Asia multi-city test."
    },
    {
        "status": "p",
        "title": "First Europe Publication",
        "abstract": "Sample publication covering a cluster of ca. 5–6 major cities around Berlin.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/europe1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POLYGON ((13.1300 52.3700, 13.6800 52.3700, 13.6800 52.6700, 13.1300 52.6700, 13.1300 52.3700)))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "Europe Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for Europe multi-city test."
    },
    {
        "status": "p",
        "title": "First North America Publication",
        "abstract": "Sample publication covering a cluster of ca. 5–6 major cities around New York City.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/northamerica1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POLYGON ((-74.1600 40.5400, -73.8100 40.5400, -73.8100 40.9400, -74.1600 40.9400, -74.1600 40.5400)))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "North America Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for North America multi-city test."
    },
    {
        "status": "p",
        "title": "First South America Publication",
        "abstract": "Sample publication covering a cluster of ca. 5–6 major cities around São Paulo.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/southamerica1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POLYGON ((-47.0000 -23.8000, -46.2500 -23.8000, -46.2500 -23.3000, -47.0000 -23.3000, -47.0000 -23.8000)))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "South America Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for South America multi-city test."
    },
    {
        "status": "p",
        "title": "First Antarctica Publication",
        "abstract": "Sample publication located in the small site-sized polygon around McMurdo Station.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/antarctica1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POLYGON ((166.60 -77.90, 166.70 -77.90, 166.70 -77.80, 166.60 -77.80, 166.60 -77.90)))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "Antarctica Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for Antarctica site test."
    },
    {
        "status": "p",
        "title": "First Australia Publication",
        "abstract": "Sample publication located in the small city-sized polygon around central Sydney.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/australia1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POLYGON ((151.20 -33.90, 151.30 -33.90, 151.30 -33.80, 151.20 -33.80, 151.20 -33.90)))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "Australia Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for Australia city test."
    },
    {
        "status": "p",
        "title": "First South Atlantic Ocean Publication",
        "abstract": "Sample publication covering a small survey area in the South Atlantic Ocean.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/southatlantic1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POLYGON ((-25.0000 -25.0000, -15.0000 -25.0000, -15.0000 -15.0000, -25.0000 -15.0000, -25.0000 -25.0000)))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "Marine Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for South Atlantic Ocean small‐area test."
    },
    {
        "status": "p",
        "title": "First Southern Ocean Publication",
        "abstract": "Sample publication covering a small research area in the Southern Ocean.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/southernocean1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POLYGON ((-30.0000 -60.0000,  30.0000 -60.0000,  30.0000 -50.0000, -30.0000 -50.0000, -30.0000 -60.0000)))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "Marine Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for Southern Ocean small‐area test."
    },
    {
        "status": "p",
        "title": "First Indian Ocean Publication",
        "abstract": "Sample publication covering a small survey area in the Indian Ocean.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/indianocean1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POLYGON ((70.0000 -10.0000, 80.0000 -10.0000, 80.0000 0.0000, 70.0000 0.0000, 70.0000 -10.0000)))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "Marine Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for Indian Ocean small‐area test."
    },
    {
        "status": "p",
        "title": "First North Pacific Ocean Publication",
        "abstract": "Sample publication covering a small sampling area in the North Pacific Ocean.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/northpacific1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POLYGON ((-140.0000 30.0000, -130.0000 30.0000, -130.0000 40.0000, -140.0000 40.0000, -140.0000 30.0000)))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "Marine Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for North Pacific Ocean small‐area test."
    },
    {
        "status": "p",
        "title": "First North Atlantic Ocean Publication",
        "abstract": "Sample publication covering a small sampling area in the North Atlantic Ocean.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/northatlantic1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POLYGON((-40.0000 30.0000, -30.0000 30.0000, -30.0000 40.0000, -40.0000 40.0000, -40.0000 30.0000)),POLYGON((-20.0000 35.0000, -10.0000 35.0000, -10.0000 45.0000, -20.0000 45.0000, -20.0000 35.0000)))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "Marine Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for North Atlantic Ocean small‐area test."
    },
    {
        "status": "p",
        "title": "First Arctic Ocean Publication",
        "abstract": "Sample publication covering a small survey area in the Arctic Ocean.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/arcticocean1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(LINESTRING(-50.0000 85.0000, 0.0000 85.0000, 50.0000 85.0000))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "Marine Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for Arctic Ocean small‐area test."
    },
    {
        "status": "p",
        "title": "First South Pacific Ocean Publication",
        "abstract": "Sample publication covering a small research area in the South Pacific Ocean.",
        "publicationDate": "2025-05-29",
        "doi": "10.5555/southpacific1",
        "url": None,
        "geometry": "SRID=4326;GEOMETRYCOLLECTION(POINT(-135.0000 -15.0000))",
        "creationDate": "2025-05-29T12:00:00Z",
        "lastUpdate":   "2025-05-29T12:00:00Z",
        "source": "Marine Test Journal",
        "timeperiod_startdate": ["2025-01-01"],
        "timeperiod_enddate":   ["2025-12-31"],
        "provenance": "Manually added for South Pacific Ocean small‐area test."
    },
]


class ExtendedPublicationFeedTests(TestCase):
    def setUp(self):
        for data in EXTENDED_TEST_DATA:
            Publication.objects.create(
                status=data["status"],
                title=data["title"],
                abstract=data["abstract"],
                publicationDate=data["publicationDate"],
                doi=data["doi"],
                url=data["url"],
                geometry=data["geometry"],
                creationDate=data["creationDate"],
                lastUpdate=data["lastUpdate"],
                source=data["source"],
                timeperiod_startdate=data["timeperiod_startdate"],
                timeperiod_enddate=data["timeperiod_enddate"],
                provenance=data["provenance"],
            )

    def test_total_publications(self):
        self.assertEqual(Publication.objects.count(), 14)

    def test_geometry_matches_expected_numbers(self):
        # 1) serialize & parse
        geojson_str = serialize(
            'geojson',
            Publication.objects.all(),
            geometry_field='geometry'
        )
        gj = json.loads(geojson_str)
        features = gj['features']

        feat_by_doi = {
            feat['properties']['doi']: feat
            for feat in features
        }

        expected = {
            '10.5555/africa1':       (3.0,   4.5),
            '10.5555/asia1':         (116.0, 39.0),
            '10.5555/europe1':       (13.13, 52.37),
            '10.5555/northamerica1': (-74.16, 40.54),
            '10.5555/southamerica1': (-47.0, -23.8),
            '10.5555/antarctica1':   (166.6, -77.9),
            '10.5555/australia1':    (151.2, -33.9),
            '10.5555/southatlantic1': (-25.0, -25.0),
            '10.5555/southernocean1': (-30.0, -60.0),
            '10.5555/indianocean1':  (70.0,  -10.0),
            '10.5555/northpacific1': (-140.0, 30.0),
            # picks first polygon’s first vertex
            '10.5555/northatlantic1': (-40.0, 30.0),
            # first point of the LineString
            '10.5555/arcticocean1':  (-50.0, 85.0),
            '10.5555/southpacific1': (-135.0, -15.0),  # the POINT itself
        }

        for doi, (exp_lon, exp_lat) in expected.items():
            with self.subTest(doi=doi):
                feat = feat_by_doi[doi]
                geom = feat['geometry']
                first_geom = geom['geometries'][0]
                coords = None

                if first_geom['type'] == 'Point':
                    coords = first_geom['coordinates']
                elif first_geom['type'] == 'LineString':
                    coords = first_geom['coordinates'][0]
                elif first_geom['type'] == 'Polygon':
                    coords = first_geom['coordinates'][0][0]
                else:
                    self.fail(
                        f"Unhandled geometry type {first_geom['type']} for DOI {doi}")

                lon, lat = coords
                self.assertAlmostEqual(lon, exp_lon, places=3,
                                       msg=f"Longitude for {doi} was {lon}, expected ~{exp_lon}")
                self.assertAlmostEqual(lat, exp_lat, places=3,
                                       msg=f"Latitude for {doi} was {lat}, expected ~{exp_lat}")
