# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

# publications/management/commands/harvest_sources.py

"""
Django management command to harvest publications from real sources.

Supports OAI-PMH, RSS/Atom and Crossref-prefix harvesting. Crossref is the
primary route for Copernicus, whose OAI-PMH endpoint has been HTTP 404 since
December 2025. Sources can be marked ``enabled: False`` to keep their config
visible but skip them on ``--all`` runs — useful for documenting upstream
outages.

Usage:
    # all currently-enabled sources
    python manage.py harvest_sources --all

    # explicit selection
    python manage.py harvest_sources --source copernicus --max-records 50
    python manage.py harvest_sources --source geo-leo --source eartharxiv

    # narrow a Crossref-prefix source to specific container titles
    python manage.py harvest_sources --source copernicus \
        --source-title "Earth System Science Data" \
        --source-title "Atmospheric Chemistry and Physics"
"""

import logging
from datetime import datetime

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.text import slugify
from django_q.tasks import async_task

from works.models import Collection, HarvestingEvent, Source, Work
from works.tasks import (
    harvest_crossref_book_list,
    harvest_crossref_prefix,
    harvest_datacite,
    harvest_geoscienceworld,
    harvest_gfz_igsn,
    harvest_mountain_wetlands,
    harvest_oai_endpoint,
    harvest_openalex_source,
    harvest_rss_endpoint,
)

logger = logging.getLogger(__name__)
User = get_user_model()

# Source configurations.
#
# `source_type` selects the harvester implementation. When a source has
# `enabled: False` it stays in this config (so the config is the
# documentation) but `--all` skips it with a warning. Use `disabled_reason`
# to explain why for `--list`.
SOURCE_CONFIG = {
    "copernicus": {
        "name": "Copernicus Publications",
        # Crossref (DOI prefix 10.5194) is the primary route: the Copernicus
        # OAI-PMH endpoint at oai-pmh.copernicus.org/oai.php has been HTTP 404
        # since December 2025. The DOI prefix is the source-of-truth filter;
        # the URL is just a display value because the Crossref task builds its
        # own params.
        "url": "https://api.crossref.org/works?filter=prefix:10.5194",
        "collection_name": "Copernicus Publications",
        "homepage_url": "https://publications.copernicus.org/",
        "publisher_name": "Copernicus Publications",
        "source_type": "crossref-prefix",
        "crossref_prefix": "10.5194",
        # Default behaviour: fetch the full abstract from the source
        # landing page rather than the Crossref-supplied <jats:p> render.
        "fetch_abstract_from_publisher": True,
        "is_oa": True,
        "default_work_type": "article",
    },
    # Copernicus journals whose titles contain a comma use issn_l for filtering
    # because Crossref treats commas inside filter= as clause separators, making
    # container-title: unable to match them.
    "copernicus-gi": {
        "name": "Geoscientific Instrumentation, Methods and Data Systems",
        # container-title filter would break at the comma; issn_l is used instead.
        # Display URL shows the actual Crossref filter applied (prefix + issn).
        "url": "https://api.crossref.org/works?filter=prefix:10.5194,issn:2193-0864",
        "collection_name": "Copernicus Publications",
        "homepage_url": "https://gi.copernicus.org/",
        "publisher_name": "Copernicus Publications",
        "source_type": "crossref-prefix",
        "crossref_prefix": "10.5194",
        "issn_l": "2193-0864",
        "fetch_abstract_from_publisher": True,
        "is_oa": True,
        "default_work_type": "article",
    },
    # AGILE GI — two publishing streams for one collection.
    # Both source keys start with `agile-gi`, so --source-prefix agile-gi
    # harvests both in one run.
    "agile-giss": {
        "name": "AGILE: GIScience Series (Crossref)",
        "openalex_id": "S4210203054",
        # Crossref-prefix harvester ignores `url` (builds its own params), but
        # we keep a representative URL for the admin UI / --list display.
        "url": "https://api.crossref.org/works?filter=prefix:10.5194,container-title:AGILE%3A+GIScience+Series",
        "collection_name": "AGILE GI",
        "collection_description": (
            "Full papers from the AGILE International Conference on Geographic Information Science, "
            "the annual meeting of the Association of Geographic Information Laboratories in Europe "
            "(AGILE, established 1998). "
            "Peer-reviewed full papers published by Springer in the Lecture Notes in Geoinformation "
            "and Cartography series (2004–2019) and in open access by Copernicus Publications "
            "in the AGILE: GIScience Series (2020–present). "
            "Short papers and poster abstracts are not included."
        ),
        "homepage_url": "https://www.agile-giscience-series.net/articles/index.html",
        "publisher_name": "Copernicus Publications",
        "source_type": "crossref-prefix",
        "crossref_prefix": "10.5194",
        # Baked-in container-title filter so an --all run reaches AGILE-GISS
        # without the operator having to remember the title string. Note the
        # colon: Crossref records the title as 'AGILE: GIScience Series'
        # (verified 2026-05-06); without the colon the filter returns zero hits.
        "source_titles": ["AGILE: GIScience Series"],
        "fetch_abstract_from_publisher": True,
        "is_oa": True,
        "default_work_type": "proceedings-article",
    },
    "agile-gi-lncs": {
        "name": "AGILE: Springer LNCS Proceedings",
        # Crossref-prefix harvester ignores `url` (builds its own params), but
        # we keep a representative URL for the admin UI / --list display.
        "url": "https://api.crossref.org/works?filter=prefix:10.1007",
        "collection_name": "AGILE GI",
        "collection_description": (
            "Full papers from the AGILE International Conference on Geographic Information Science, "
            "the annual meeting of the Association of Geographic Information Laboratories in Europe "
            "(AGILE, established 1998). "
            "Peer-reviewed full papers published by Springer in the Lecture Notes in Geoinformation "
            "and Cartography series (2004–2019) and in open access by Copernicus Publications "
            "in the AGILE: GIScience Series (2020–present). "
            "Short papers and poster abstracts are not included."
        ),
        "homepage_url": "https://agile-gi.eu/past-conferences/proceedings",
        "publisher_name": "Springer",
        "source_type": "crossref-prefix",
        "crossref_prefix": "10.1007",
        "doi_prefix": "10.1007",
        # One ISBN per AGILE Springer volume (2008–2019). Earlier conferences
        # (pre-2008) were not published in the Springer LNCS/LNGiC series.
        # ISBNs verified from https://agile-gi.eu/past-conferences/proceedings.
        "book_isbns": [
            "978-3-540-78945-1",  # 2008 — The European Information Society
            "978-3-642-00317-2",  # 2009 — Advances in GIScience
            "978-3-642-12325-2",  # 2010 — Geospatial Thinking
            "978-3-642-19788-8",  # 2011 — Advancing Geoinformation Science for a Changing World
            "978-3-642-29062-6",  # 2012 — Bridging the Geographic Information Sciences
            "978-3-319-00615-4",  # 2013 — Geographic Information Science at the Heart of Europe
            "978-3-319-03610-6",  # 2014 — Connecting a Digital Europe Through Location and Place
            "978-3-319-16787-9",  # 2015 — Geographic Information Science as an Enabler of Smarter Cities
            "978-3-319-33782-1",  # 2016 — Geospatial Data in a Changing World
            "978-3-319-56759-4",  # 2017 — Societal Geo-innovation
            "978-3-319-78208-9",  # 2018 — Geospatial Technologies for All
            "978-3-030-14745-7",  # 2019 — Geospatial Technologies for Local and Regional Development
        ],
        "fetch_abstract_from_publisher": True,
        "is_oa": False,
        "default_work_type": "proceedings-article",
    },
    "geo-leo": {
        "name": "GEO-LEO e-docs",
        "url": "https://e-docs.geo-leo.de/server/oai/request?verb=ListRecords&metadataPrefix=oai_dc",
        "collection_name": "GEO-LEO",
        "homepage_url": "https://e-docs.geo-leo.de/",
        "publisher_name": "GEO-LEO",
        "source_type": "oai-pmh",
        "is_oa": True,
        "default_work_type": "article",
    },
    "eartharxiv": {
        "name": "EarthArXiv",
        "url": "https://eartharxiv.org/api/oai/?verb=ListRecords&metadataPrefix=oai_dc",
        "collection_name": "EarthArXiv",
        "homepage_url": "https://eartharxiv.org/",
        "publisher_name": "California Digital Library",
        "source_type": "oai-pmh",
        "is_oa": True,
        "is_preprint": True,
        "default_work_type": "preprint",
        # OpenAlex tracks EarthArXiv under two entries due to its platform migration from
        # OSF to CDL; S4306401273 (OSF Preprints) has indexed works, S4306402614 (CDL) has 0.
        "openalex_id": "S4306401273",
    },
    "scientific-data": {
        "name": "Scientific Data",
        # Display URL only — harvester builds its own Crossref API params.
        "url": "https://api.crossref.org/works?filter=prefix:10.1038,container-title:Scientific%20Data",
        "collection_name": "Scientific Data",
        "homepage_url": "https://www.nature.com/sdata/",
        "publisher_name": "Nature Publishing Group",
        "source_type": "crossref-prefix",
        "openalex_id": "S2607323502",
        "crossref_prefix": "10.1038",
        # doi_prefix must also be set so _reconcile_source populates Source.doi_prefix;
        # auto-scheduled Django-Q runs don't receive a prefix= argument and fall back
        # to source.doi_prefix — without it they would use the hardcoded "10.5194" default.
        "doi_prefix": "10.1038",
        # Baked-in title filter; 10.1038 covers all of Springer Nature, so this
        # is essential to restrict results to Scientific Data only.
        "source_titles": ["Scientific Data"],
        # Crossref carries complete abstracts for Springer Nature; scraping
        # nature.com at scale risks Cloudflare blocks.
        "fetch_abstract_from_publisher": False,
        "is_oa": True,
        "default_work_type": "dataset",
    },
    "essoar": {
        "name": "ESS Open Archive",
        # Display URL only — harvester builds its own Crossref API params.
        "url": "https://api.crossref.org/works?filter=member:311,type:posted-content",
        "collection_name": "ESS Open Archive",
        "collection_description": (
            "Preprints, posters and presentations from AGU's Earth and Space "
            "Science Open Archive (ESSOAr), harvested via Crossref."
        ),
        "homepage_url": "https://essopenarchive.org/",
        "publisher_name": "American Geophysical Union",
        "source_type": "crossref-prefix",
        # ESSOAr has no usable native API (Atypon/Cloudflare) and spans TWO DOI
        # eras: 10.1002/essoar.* (2018–2022, original platform) and
        # 10.22541/essoar.* (2022–present, Authorea). No single prefix covers
        # both, but both are Wiley Crossref member 311, work type posted-content.
        # We harvest that slice (~94k incl. Authorea) and keep only DOIs that
        # contain "essoar" — capturing the complete ESSOAr corpus, both eras.
        "crossref_filter": "member:311,type:posted-content",
        "doi_contains": "essoar",
        # essopenarchive.org sits behind Cloudflare (landing pages not scrapable);
        # rely on the Crossref JATS abstract (+ async OpenAIRE enrichment).
        "fetch_abstract_from_publisher": False,
        "is_oa": True,
        "is_preprint": True,
        "default_work_type": "preprint",
    },
    "mountain-wetlands": {
        "name": "Mountain Wetlands Repository",
        "url": "https://andes.mountain-wetlands-repository.info/api/v1/items/",
        "collection_name": "Mountain Wetlands",
        "homepage_url": "https://andes.mountain-wetlands-repository.info/",
        "publisher_name": "Mountain Wetlands Repository (MaRESS)",
        "source_type": "mountain-wetlands",
        "is_oa": True,
        "default_work_type": "article",
    },
    # GeoScienceWorld (GSW) — journals from multiple geoscience publishers hosted on
    # pubs.geoscienceworld.org. Articles include GeoRef coordinates as WKT on landing
    # pages; geoextent's GSW content provider extracts them via Cloudflare bypass.
    # Temporal/epoch extraction is deferred — see issue #257 / geoextent#122.
    # Use --source-prefix gsw to harvest all sources in one run.
    "gsw-seg": {
        "name": "GeoScienceWorld — SEG Journals",
        "url": "https://pubs.geoscienceworld.org/seg",
        "collection_name": "GeoScienceWorld SEG",
        "homepage_url": "https://pubs.geoscienceworld.org/seg",
        "publisher_name": "Society of Exploration Geophysicists",
        "source_type": "geoscienceworld",
        "doi_prefix": "10.1190",
        "default_work_type": "article",
    },
    "gsw-gsl": {
        "name": "GeoScienceWorld — Geological Society of London",
        "url": "https://pubs.geoscienceworld.org/gsl",
        "collection_name": "GeoScienceWorld GSL",
        "homepage_url": "https://pubs.geoscienceworld.org/gsl",
        "publisher_name": "Geological Society of London",
        "source_type": "geoscienceworld",
        "doi_prefix": "10.1144",
        "default_work_type": "article",
    },
    "gsw-mineralogical": {
        "name": "GeoScienceWorld — Mineralogical Society",
        "url": "https://pubs.geoscienceworld.org/minersoc",
        "collection_name": "GeoScienceWorld Mineralogical Society",
        "homepage_url": "https://pubs.geoscienceworld.org/minersoc",
        "publisher_name": "Mineralogical Society of Great Britain and Ireland",
        "source_type": "geoscienceworld",
        "doi_prefix": "10.1180",
        "default_work_type": "article",
    },
    "gsw-gsa": {
        "name": "GeoScienceWorld — Geological Society of America",
        "url": "https://pubs.geoscienceworld.org/gsa",
        "collection_name": "GeoScienceWorld GSA",
        "homepage_url": "https://pubs.geoscienceworld.org/gsa",
        "publisher_name": "Geological Society of America",
        "source_type": "geoscienceworld",
        "doi_prefix": "10.1130",
        "default_work_type": "article",
    },
    "gsw-own": {
        "name": "GeoScienceWorld — Aggregated (10.2113)",
        "url": "https://pubs.geoscienceworld.org",
        "collection_name": "GeoScienceWorld Aggregated",
        "homepage_url": "https://pubs.geoscienceworld.org",
        "publisher_name": "GeoScienceWorld",
        "source_type": "geoscienceworld",
        "doi_prefix": "10.2113",
        "default_work_type": "article",
    },
    "gsw-aapg": {
        "name": "GeoScienceWorld — AAPG/Datapages",
        "url": "https://pubs.geoscienceworld.org/aapg",
        "collection_name": "GeoScienceWorld AAPG",
        "homepage_url": "https://pubs.geoscienceworld.org/aapg",
        "publisher_name": "AAPG/Datapages",
        "source_type": "geoscienceworld",
        "doi_prefix": "10.1306",
        "default_work_type": "article",
    },
    "gsw-seg-econ": {
        "name": "GeoScienceWorld — Society of Economic Geologists",
        "url": "https://pubs.geoscienceworld.org/seg-econ",
        "collection_name": "GeoScienceWorld SEG-Econ",
        "homepage_url": "https://pubs.geoscienceworld.org/seg-econ",
        "publisher_name": "Society of Economic Geologists",
        "source_type": "geoscienceworld",
        "doi_prefix": "10.5382",
        "default_work_type": "article",
    },
    "gsw-clay": {
        "name": "GeoScienceWorld — Clay Minerals Society",
        "url": "https://pubs.geoscienceworld.org/clay",
        "collection_name": "GeoScienceWorld Clay Minerals",
        "homepage_url": "https://pubs.geoscienceworld.org/clay",
        "publisher_name": "Clay Minerals Society",
        "source_type": "geoscienceworld",
        "doi_prefix": "10.1346",
        "default_work_type": "article",
    },
    "gsw-cushman": {
        "name": "GeoScienceWorld — Cushman Foundation for Foraminiferal Research",
        "url": "https://pubs.geoscienceworld.org/cushman",
        "collection_name": "GeoScienceWorld Cushman Foundation",
        "homepage_url": "https://pubs.geoscienceworld.org/cushman",
        "publisher_name": "Cushman Foundation for Foraminiferal Research",
        "source_type": "geoscienceworld",
        "doi_prefix": "10.61551",
        "default_work_type": "article",
    },
    # Pensoft / ARPHA platform journals — OAI-PMH endpoint pattern:
    #   https://<subdomain>.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=<set>
    # All entries confirmed to embed schema:contentLocation GeoCoordinates JSON-LD
    # in article pages. Coverage is article-type-dependent: data papers and taxonomic
    # revisions ~80–100%; reviews and methods articles ~0%.
    # Full audit of all 130 ARPHA OAI sets conducted 2026-06-09.
    # Use --source-prefix pensoft to harvest all pensoft-* sources in one run.
    "pensoft-bdj": {
        "name": "Biodiversity Data Journal",
        "url": "https://bdj.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=bdj",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://bdj.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "1314-2828",
        "openalex_id": "S2764367193",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-zookeys": {
        "name": "ZooKeys",
        "url": "https://zookeys.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=zookeys",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://zookeys.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "1313-2970",
        "openalex_id": "S199213172",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-phytokeys": {
        "name": "PhytoKeys",
        "url": "https://phytokeys.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=phytokeys",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://phytokeys.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "1314-2003",
        "openalex_id": "S138605562",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-neobiota": {
        "name": "NeoBiota",
        "url": "https://neobiota.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=neobiota",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://neobiota.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "1314-2488",
        "openalex_id": "S4210189550",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-mycokeys": {
        "name": "MycoKeys",
        "url": "https://mycokeys.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=mycokeys",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://mycokeys.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "1314-4049",
        "openalex_id": "S4210227917",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-herpetozoa": {
        "name": "Herpetozoa",
        "url": "https://herpetozoa.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=herpetozoa",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://herpetozoa.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "2682-955X",
        "openalex_id": "S4210228833",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-natureconservation": {
        "name": "Nature Conservation",
        "url": "https://natureconservation.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=natureconservation",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://natureconservation.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "1314-3301",
        "openalex_id": "S2764730374",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-alpineentomology": {
        "name": "Alpine Entomology",
        "url": "https://alpineentomology.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=alpineentomology",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://alpineentomology.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "2535-0889",
        "openalex_id": "S4210217666",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-oneecosystem": {
        "name": "One Ecosystem",
        "url": "https://oneecosystem.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=oneecosystem",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://oneecosystem.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "2367-8194",
        "openalex_id": "S4210213968",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-evolsyst": {
        "name": "Evolutionary Systematics",
        "url": "https://evolsyst.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=evolsyst",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://evolsyst.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "2535-0730",
        "openalex_id": "S4210215492",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-mbmg": {
        "name": "Metabarcoding and Metagenomics",
        "url": "https://mbmg.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=mbmg",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://mbmg.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "2534-9708",
        "openalex_id": "S4210182883",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-neotropical": {
        "name": "Neotropical Biology and Conservation",
        "url": "https://neotropical.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=neotropical",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://neotropical.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "2236-3777",
        "openalex_id": "S4210214477",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-caucasiana": {
        "name": "Caucasiana",
        "url": "https://caucasiana.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=caucasiana",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://caucasiana.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "2667-9809",
        "openalex_id": "S4210198213",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-italianbotanist": {
        "name": "Italian Botanist",
        "url": "https://italianbotanist.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=italianbotanist",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://italianbotanist.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "2531-4033",
        "openalex_id": "S4210221877",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-abs": {
        "name": "Acta Biologica Sibirica",
        "url": "https://abs.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=abs",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://abs.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "2412-1908",
        "openalex_id": "S2737068255",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-vdj": {
        "name": "Viticulture Data Journal",
        "url": "https://vdj.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=vdj",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://vdj.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "2603-431X",
        "openalex_id": "S4210212065",
        "is_oa": True,
        "default_work_type": "article",
    },
    "biosystecol": {
        "name": "Biosystematics and Ecology",
        "url": "https://biosystecol.oeaw.ac.at/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=biosystecol",
        "collection_name": "Biosystematics and Ecology",
        "homepage_url": "https://biosystecol.oeaw.ac.at/",
        "publisher_name": "Austrian Academy of Sciences",
        "source_type": "oai-pmh",
        "issn_l": "1026-4949",
        "openalex_id": "S4389157932",
        "is_oa": True,
        "default_work_type": "article",
    },
    "bulletinofinsectology": {
        "name": "Bulletin of Insectology",
        "url": "https://bulletinofinsectology.org/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=bulletinofinsectology",
        "collection_name": "Bulletin of Insectology",
        "homepage_url": "https://bulletinofinsectology.org/",
        "publisher_name": "University of Bologna",
        "source_type": "oai-pmh",
        "issn_l": "1721-8861",
        "openalex_id": "S13822188",
        "is_oa": True,
        "default_work_type": "article",
    },
    "pensoft-nhcm": {
        "name": "Natural History Collections and Museomics",
        "url": "https://nhcm.pensoft.net/oai.php?verb=ListRecords&metadataPrefix=oai_dc&set=nhcm",
        "collection_name": "Pensoft Publishers",
        "homepage_url": "https://nhcm.pensoft.net/",
        "publisher_name": "Pensoft Publishers",
        "source_type": "oai-pmh",
        "issn_l": "3033-0955",
        "openalex_id": "S5407045911",
        "is_oa": True,
        "default_work_type": "article",
    },
    "sesar-igsn": {
        "name": "SESAR IGSN samples (DataCite)",
        # DataCite-harvester sources use url only for display; the harvester
        # builds its API query from harvest_config.
        "url": "https://api.datacite.org/dois?query=types.resourceTypeGeneral:PhysicalObject",
        "collection_name": "SESAR IGSN samples",
        "collection_description": (
            "Physical samples registered with IGSN via SESAR (System for Earth Sample Registration), "
            "harvested from DataCite. Each sample carries authoritative coordinates; where the sample "
            "metadata links to publications or datasets, those are recorded as related works."
        ),
        "homepage_url": "https://www.geosamples.org/",
        "publisher_name": "SESAR",
        "source_type": "datacite",
        "is_oa": True,
        "default_work_type": "physical-object",
        # Default sampling: SESAR, coordinates required, stable sort. Cap runs
        # with --max-records or a "max_records" key here. Override any of
        # publisher / require_coordinates / only_with_related_doi / bbox /
        # date_from / date_to / resource_type / query / sort per source.
        "harvest_config": {
            "publisher": "SESAR",
            "require_coordinates": True,
            "sort": "created",
        },
        # Large corpus — disabled from --all by default; harvest explicitly with
        # --source sesar-igsn --max-records N.
        "enabled": False,
    },
    "gfz-igsn": {
        "name": "GFZ IGSN samples (landing-page scrape)",
        "url": "https://dataservices.gfz-potsdam.de/igsn/",
        "collection_name": "GFZ IGSN samples",
        "collection_description": (
            "Physical samples registered with IGSN by GFZ Data Services. GFZ does not publish "
            "coordinates to DataCite, so this source scrapes each sample's landing page for its "
            "sampling location (~65% of samples carry usable coordinates)."
        ),
        "homepage_url": "https://dataservices.gfz-potsdam.de/igsn/",
        "publisher_name": "GFZ Data Services",
        "source_type": "gfz-igsn",
        "is_oa": True,
        "default_work_type": "physical-object",
        # ~38k landing-page fetches; throttle politely. Cap runs with --max-records.
        "harvest_config": {"throttle": 0.5},
        "enabled": False,
    },
}


def _is_enabled(config):
    """Sources without an `enabled` key default to True for back-compat."""
    return config.get("enabled", True)


def _get_or_create_collection(config):
    """Return the Collection matching ``config['collection_name']``, creating it on first use.

    If ``collection_description`` is present in ``config`` it is written to
    ``Collection.description`` both on creation and on every subsequent call
    (e.g. ``--insert-sources`` re-runs), so the description stays current
    without requiring a manual admin edit.

    New collections start **unpublished** (matching the model default and the
    harvest-time auto-create path in ``works/harvesting/common.py``): an
    operator curates the name/description and flips ``is_published`` when the
    collection is ready to be public. The deployment update process re-runs
    ``--insert-sources`` on every update, so auto-publishing here would re-expose
    every built-in collection on a fresh database before anyone has reviewed it.

    Every other field — in particular ``is_published`` — lives only in
    ``defaults`` and is therefore applied **once, at creation**. Re-runs must
    not clobber admin/curator-managed state, so a collection an operator has
    published (or unpublished) stays that way across updates. The description
    save below is scoped with ``update_fields`` to keep that guarantee.
    """
    name = config.get("collection_name")
    if not name:
        return None
    identifier = slugify(name)[:100] or "collection"
    description = config.get("collection_description", "")
    collection, created = Collection.objects.get_or_create(
        identifier=identifier,
        defaults={
            "name": name,
            "description": description,
            "homepage_url": config.get("homepage_url") or None,
            # Start unpublished; an admin reviews and publishes explicitly.
            "is_published": False,
        },
    )
    if not created and description and collection.description != description:
        collection.description = description
        collection.save(update_fields=["description"])
    return collection


class Command(BaseCommand):
    help = "Harvest publications from real sources into the current database"

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            action="append",
            choices=list(SOURCE_CONFIG.keys()),
            help=(f"Source to harvest (choices: {', '.join(SOURCE_CONFIG.keys())}). Can be specified multiple times."),
        )
        parser.add_argument(
            "--source-title",
            action="append",
            default=None,
            help=(
                "For Crossref-prefix sources, narrow the harvest to specific "
                "container-title strings. Repeat the flag for multiple titles. "
                "Ignored for OAI-PMH and RSS sources."
            ),
        )
        parser.add_argument(
            "--source-issn",
            dest="source_issn",
            default=None,
            help=(
                "For Crossref-prefix sources, narrow the harvest to a specific journal "
                "by ISSN (e.g. 2193-0864). Overrides the source's own issn_l. "
                "Use when the journal title contains a comma and --source-title fails."
            ),
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Harvest from all enabled sources (skips entries marked enabled: False)",
        )
        parser.add_argument(
            "--include-disabled",
            action="store_true",
            help="When combined with --all, also attempt disabled sources (rarely useful — disabled means upstream is broken)",
        )
        parser.add_argument(
            "--max-records",
            type=int,
            default=None,
            help=(
                "Maximum number of records to harvest per source (default: unlimited). "
                "For OAI-PMH sources, harvesting proceeds newest-year-first; "
                "--max-records stops as soon as the limit is reached, so the oldest "
                "year visited may be only partially harvested. The harvest summary "
                "marks such years as '(partial)'."
            ),
        )
        parser.add_argument(
            "--update",
            action="store_true",
            help=(
                "Update same-source duplicates in place instead of skipping "
                "them. Geometry and temporal metadata on the existing Work are "
                "preserved if the new harvest brings nothing for those fields "
                "(typically because they were contributed by users via "
                "OPTIMAP, not the source). Status and created_by are never "
                'overwritten. A "harvest_update" event is appended to '
                "Work.provenance.events."
            ),
        )
        parser.add_argument(
            "--full",
            action="store_true",
            help=(
                "For Crossref-prefix sources, force a complete backfill: ignore "
                "the last completed harvest event and re-walk the entire prefix "
                "instead of only records re-indexed since then. Use this to "
                "recover the full catalogue without deleting HarvestingEvent "
                "rows by hand. Mutually exclusive with --since. Ignored by "
                "non-Crossref sources."
            ),
        )
        parser.add_argument(
            "--since",
            type=str,
            default=None,
            help=(
                "For Crossref-prefix sources, set an explicit from-update-date "
                "window (YYYY-MM-DD): only records Crossref re-indexed on or "
                "after this date are returned. Overrides the window derived from "
                "the last completed harvest. Mutually exclusive with --full. "
                "Ignored by non-Crossref sources."
            ),
        )
        parser.add_argument(
            "--no-publisher-abstract",
            action="store_true",
            help=(
                "For Crossref-prefix sources, skip the publisher-side "
                "landing-page fetch and use the Crossref-supplied abstract "
                "as-is (faster, but loses formatting and is sometimes "
                "incomplete). Default: fetch the canonical abstract from "
                "the publisher."
            ),
        )
        parser.add_argument(
            "--create-sources",
            action="store_true",
            help="Create Source entries if they don't exist (default: use existing sources only)",
        )
        parser.add_argument(
            "--async",
            dest="async_mode",
            action="store_true",
            help=(
                "Enqueue each harvest as a Django-Q task instead of running it "
                "synchronously. Requires a running qcluster (python manage.py "
                "qcluster); without one the tasks sit in the broker queue and "
                "never execute. Prints the enqueued task id per source and "
                "returns immediately — the per-source statistics summary is "
                "skipped because results are not available yet (watch the "
                "HarvestingEvent rows / harvest-completion emails instead)."
            ),
        )
        parser.add_argument(
            "--user-email",
            type=str,
            default=None,
            help="Email of user to associate with harvesting events (optional)",
        )
        parser.add_argument(
            "--source-prefix",
            dest="source_prefix",
            default=None,
            help=(
                "Harvest all enabled sources whose key starts with this prefix "
                "(e.g. --source-prefix pensoft selects all pensoft-* sources). "
                "Can be combined with --include-disabled."
            ),
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="List available sources and exit",
        )
        parser.add_argument(
            "--insert-sources",
            action="store_true",
            help=(
                "Insert all sources from SOURCE_CONFIG as Source rows (so they "
                "show up in the Django admin and can be triggered from there) "
                "and exit without harvesting. Existing rows (matched by name or "
                "URL) are left untouched. Disabled sources are skipped unless "
                "--include-disabled is also given."
            ),
        )

    def handle(self, *args, **options):
        # List sources and exit
        if options["list"]:
            self.stdout.write(self.style.SUCCESS("\nAvailable sources for harvesting:\n"))
            for key, config in SOURCE_CONFIG.items():
                source_type = config.get("source_type", "oai-pmh").upper()
                is_preprint = " (preprint)" if config.get("is_preprint", False) else ""
                work_type = config.get("default_work_type", "article")
                marker = "" if _is_enabled(config) else " [DISABLED]"
                self.stdout.write(f"  {key:15} - {config['name']}{is_preprint}{marker}")
                self.stdout.write(
                    f"                  Type: {source_type}, Work Type: {work_type}, URL: {config['homepage_url']}"
                )
                if not _is_enabled(config) and config.get("disabled_reason"):
                    self.stdout.write(self.style.WARNING(f"                  Reason: {config['disabled_reason']}"))
            return

        include_disabled = options["include_disabled"]
        source_titles = options["source_title"]
        source_issn = options["source_issn"]
        no_publisher_abstract = options["no_publisher_abstract"]
        full_backfill = options["full"]
        since = options["since"]

        if full_backfill and since:
            raise CommandError("--full and --since are mutually exclusive; pass only one.")
        if since:
            try:
                datetime.strptime(since, "%Y-%m-%d")
            except ValueError:
                raise CommandError(f"--since must be a YYYY-MM-DD date, got {since!r}.")

        # Bulk-insert sources and exit (no harvesting)
        if options["insert_sources"]:
            self._insert_sources(include_disabled=include_disabled)
            return

        # Determine which sources to harvest
        if options["all"]:
            if include_disabled:
                sources_to_harvest = list(SOURCE_CONFIG.keys())
            else:
                sources_to_harvest = [k for k, c in SOURCE_CONFIG.items() if _is_enabled(c)]
        elif options["source"]:
            sources_to_harvest = options["source"]
        elif options["source_prefix"]:
            prefix = options["source_prefix"]
            sources_to_harvest = [
                k for k, c in SOURCE_CONFIG.items() if k.startswith(prefix) and (_is_enabled(c) or include_disabled)
            ]
            if not sources_to_harvest:
                raise CommandError(
                    f"No {'enabled ' if not include_disabled else ''}sources match "
                    f"prefix '{prefix}'. Use --list to see available sources."
                )
        else:
            raise CommandError(
                "Please specify --all to harvest all enabled sources, "
                "--source-prefix <prefix> for a group, or "
                "--source <name> for specific sources.\n"
                "Use --list to see available sources."
            )

        # Get user if specified
        user = None
        if options["user_email"]:
            try:
                user = User.objects.get(email=options["user_email"])
                self.stdout.write(f"Using user: {user.email}")
            except User.DoesNotExist:
                raise CommandError(f"User with email '{options['user_email']}' does not exist")

        max_records = options["max_records"]
        create_sources = options["create_sources"]
        update_existing = options.get("update", False)
        async_mode = options.get("async_mode", False)

        if async_mode:
            self.stdout.write(
                self.style.WARNING(
                    "--async: enqueuing harvests as Django-Q tasks. Ensure a qcluster "
                    "is running (python manage.py qcluster), or the tasks will not execute."
                )
            )
            # Fail fast: every harvest-affecting option the user explicitly set
            # must actually land in the enqueued task kwargs for every selected
            # source. The sync path silently ignores crossref-only options
            # (--source-title, --no-publisher-abstract) on non-crossref sources;
            # async refuses to drop them quietly and stops instead.
            self._validate_async_coverage(
                sources_to_harvest=sources_to_harvest,
                include_disabled=include_disabled,
                user=user,
                max_records=max_records,
                source_titles=source_titles,
                source_issn=source_issn,
                no_publisher_abstract=no_publisher_abstract,
                update_existing=update_existing,
                full_backfill=full_backfill,
                since=since,
            )

        # Summary statistics
        total_harvested = 0
        total_failed = 0
        total_skipped = 0
        results = []

        self.stdout.write(self.style.SUCCESS(f"\n{'=' * 70}"))
        self.stdout.write(self.style.SUCCESS(f"Starting harvest of {len(sources_to_harvest)} source(s)"))
        self.stdout.write(self.style.SUCCESS(f"{'=' * 70}\n"))

        # Harvest each source
        for source_key in sources_to_harvest:
            config = SOURCE_CONFIG[source_key]

            # Skip explicitly-disabled sources unless the operator opted in.
            if not _is_enabled(config) and not include_disabled:
                self.stdout.write(self.style.WARNING(f"\n--- Skipping disabled source: {config['name']} ---"))
                if config.get("disabled_reason"):
                    self.stdout.write(f"  Reason: {config['disabled_reason']}")
                total_skipped += 1
                results.append(
                    {
                        "source": config["name"],
                        "status": "skipped",
                        "count": 0,
                    }
                )
                continue

            self.stdout.write(self.style.WARNING(f"\n--- Harvesting: {config['name']} ---"))
            self.stdout.write(f"URL: {config['url']}")
            if max_records:
                self.stdout.write(f"Max records: {max_records}")

            harvest_start = timezone.now()
            try:
                # Find or create source
                source = self._get_or_create_source(config, create_sources)

                # Harvest based on source type
                source_type = config.get("source_type", "oai-pmh")
                oai_result = None

                # Async route: enqueue the same task target the recurring
                # Django-Q schedules use, then move on. We can't report
                # per-source stats because the work hasn't run yet.
                if async_mode:
                    task_path, task_kwargs = self._build_harvest_spec(
                        config=config,
                        source_type=source_type,
                        user=user,
                        max_records=max_records,
                        source_titles=source_titles,
                        source_issn=source_issn,
                        no_publisher_abstract=no_publisher_abstract,
                        update_existing=update_existing,
                        full_backfill=full_backfill,
                        since=since,
                    )
                    # Pre-create the HarvestingEvent so its PK can be printed
                    # now and matched against the row that appears in the Django
                    # admin once the task runs (the task reuses this event via
                    # start_harvesting_event rather than creating its own). The
                    # Django-Q task id is internal to the queue and never lands
                    # on a model the admin shows, so the event PK is the durable
                    # correlation handle.
                    event = HarvestingEvent.objects.create(source=source, status="pending")
                    task_kwargs["event_id"] = event.id
                    task_id = async_task(task_path, source.id, **task_kwargs)
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  Enqueued {task_path} — HarvestingEvent #{event.id} "
                            f"(Django admin), Django-Q task id: {task_id}"
                        )
                    )
                    results.append(
                        {
                            "source": config["name"],
                            "status": "queued",
                            "count": 0,
                            "task_id": task_id,
                            "event_id": event.id,
                        }
                    )
                    continue

                if update_existing:
                    self.stdout.write(
                        "  --update: same-source duplicates will be updated in place "
                        "(geometry/temporal preserved when new harvest is empty)."
                    )

                if source_type == "rss":
                    self.stdout.write("Source type: RSS/Atom")
                    harvest_rss_endpoint(
                        source.id,
                        user=user,
                        max_records=max_records,
                        update_existing=update_existing,
                    )
                elif source_type == "mountain-wetlands":
                    self.stdout.write("Source type: Mountain Wetlands Repository (MaRESS)")
                    harvest_mountain_wetlands(
                        source.id,
                        user=user,
                        max_records=max_records,
                        update_existing=update_existing,
                    )
                elif source_type == "crossref-prefix":
                    book_isbns = config.get("book_isbns")
                    if book_isbns:
                        # ISBN-per-book mode: each ISBN is one Crossref request.
                        # Used for AGILE Springer LNCS (one book per year).
                        self.stdout.write("Source type: Crossref by book ISBN list")
                        self.stdout.write(f"  ISBNs: {len(book_isbns)} volume(s)")
                        harvest_crossref_book_list(
                            source.id,
                            user=user,
                            max_records=max_records,
                            book_isbns=book_isbns,
                            update_existing=update_existing,
                        )
                    else:
                        self.stdout.write("Source type: Crossref by DOI prefix")
                        # CLI --source-title takes precedence; otherwise fall back
                        # to titles baked into the config (e.g. agile-giss).
                        effective_titles = source_titles or config.get("source_titles")
                        if effective_titles:
                            self.stdout.write(f"  Filtering to titles: {', '.join(effective_titles)}")
                        fetch_abstract = (
                            config.get("fetch_abstract_from_publisher", True) and not no_publisher_abstract
                        )
                        self.stdout.write(
                            f"  Fetch abstract from publisher landing page: {'yes' if fetch_abstract else 'no'}"
                        )
                        if full_backfill:
                            self.stdout.write("  --full: full backfill (ignoring last harvest date).")
                        elif since:
                            self.stdout.write(f"  --since: only records re-indexed on or after {since}.")
                        harvest_crossref_prefix(
                            source.id,
                            user=user,
                            max_records=max_records,
                            source_titles=effective_titles,
                            prefix=config.get("crossref_prefix"),
                            fetch_abstract_from_publisher=fetch_abstract,
                            update_existing=update_existing,
                            full=full_backfill,
                            since=since,
                            issn=source_issn,
                        )
                elif source_type == "geoscienceworld":
                    self.stdout.write("Source type: GeoScienceWorld (Crossref + geoextent)")
                    effective_prefix = config.get("doi_prefix") or (source.doi_prefix if source else None)
                    self.stdout.write(f"  DOI prefix: {effective_prefix}")
                    harvest_geoscienceworld(
                        source.id,
                        user=user,
                        max_records=max_records,
                        update_existing=update_existing,
                    )
                elif source_type == "openalex":
                    self.stdout.write("Source type: OpenAlex source")
                    self.stdout.write(f"  OpenAlex source ID: {config.get('openalex_id', '<from source row>')}")
                    harvest_openalex_source(
                        source.id,
                        user=user,
                        max_records=max_records,
                        update_existing=update_existing,
                    )
                elif source_type == "datacite":
                    self.stdout.write("Source type: DataCite (IGSN samples)")
                    harvest_datacite(
                        source.id,
                        user=user,
                        max_records=max_records,
                        update_existing=update_existing,
                    )
                elif source_type == "gfz-igsn":
                    self.stdout.write("Source type: GFZ IGSN (landing-page scrape)")
                    harvest_gfz_igsn(
                        source.id,
                        user=user,
                        max_records=max_records,
                        update_existing=update_existing,
                    )
                else:
                    # Covers source_type in {oai-pmh, ojs, janeway} — all share the OAI harvester.
                    self.stdout.write(f"Source type: {source_type}")
                    oai_result = harvest_oai_endpoint(
                        source.id,
                        user=user,
                        max_records=max_records,
                        update_existing=update_existing,
                    )

                # Get results
                event = HarvestingEvent.objects.filter(source=source).latest("started_at")
                pub_count = Work.objects.filter(job=event).count()
                skipped_count = event.records_skipped or 0
                spatial_count = Work.objects.filter(job=event).exclude(geometry__isnull=True).count()
                temporal_count = (
                    Work.objects.filter(job=event)
                    .exclude(timeperiod_startdate__isnull=True)
                    .exclude(timeperiod_startdate=[])
                    .count()
                )

                duration = (timezone.now() - harvest_start).total_seconds()

                if event.status == "completed":
                    total_harvested += pub_count
                    source.refresh_from_db(fields=["statistics"])
                    src_stats = source.statistics or {}
                    result = {
                        "source": config["name"],
                        "status": "success",
                        "count": pub_count,
                        "skipped": skipped_count,
                        "spatial": spatial_count,
                        "temporal": temporal_count,
                        "duration": duration,
                        "visited_years": [],
                        "partial_year": None,
                        "openalex_works_count": src_stats.get("openalex_works_count"),
                        "openalex_fetched_at": (src_stats.get("openalex_fetched_at") or "")[:10],
                        "oai_works_count": src_stats.get("oai_works_count"),
                        "oai_fetched_at": (src_stats.get("oai_fetched_at") or "")[:10],
                        "crossref_works_count": src_stats.get("crossref_works_count"),
                        "crossref_fetched_at": (src_stats.get("crossref_fetched_at") or "")[:10],
                    }
                    if source_type in ("oai-pmh", "ojs", "janeway") and oai_result:
                        result["visited_years"] = oai_result.get("visited_years", [])
                        result["partial_year"] = oai_result.get("partial_year")
                    results.append(result)
                else:
                    total_failed += 1
                    results.append(
                        {
                            "source": config["name"],
                            "status": "failed",
                            "count": 0,
                            "skipped": skipped_count,
                            "spatial": spatial_count,
                            "temporal": temporal_count,
                            "duration": duration,
                            "visited_years": [],
                            "partial_year": None,
                        }
                    )

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"✗ Error: {str(e)}"))
                logger.exception(f"Failed to harvest {source_key}")
                total_failed += 1
                results.append(
                    {
                        "source": config["name"],
                        "status": "error",
                        "count": 0,
                        "skipped": 0,
                        "spatial": 0,
                        "temporal": 0,
                        "duration": (timezone.now() - harvest_start).total_seconds(),
                        "visited_years": [],
                        "partial_year": None,
                        "error": str(e),
                    }
                )

        # Print consolidated summary (single report — no per-source inline results above)
        self.stdout.write(self.style.SUCCESS(f"\n{'=' * 70}"))
        self.stdout.write(self.style.SUCCESS("Harvest Summary"))
        self.stdout.write(self.style.SUCCESS(f"{'=' * 70}"))

        for result in results:
            self.stdout.write("")
            if result["status"] == "success":
                pub = result["count"]
                skipped = result.get("skipped", 0)
                spatial = result.get("spatial", 0)
                temporal = result.get("temporal", 0)
                dur = result["duration"]
                self.stdout.write(
                    self.style.SUCCESS(f"✓ {result['source']}  —  {pub} new, {skipped} skipped  ({dur:.1f}s)")
                )
                self.stdout.write(f"  Spatial: {spatial}/{pub}  |  Temporal: {temporal}/{pub}")
                oa_count = result.get("openalex_works_count")
                if oa_count is not None:
                    oa_date = result.get("openalex_fetched_at", "")
                    self.stdout.write(f"  OpenAlex: {oa_count:,} total works (as of {oa_date})")
                oai_count = result.get("oai_works_count")
                if oai_count is not None:
                    oai_date = result.get("oai_fetched_at", "")
                    self.stdout.write(f"  OAI-PMH:  {oai_count:,} total records (as of {oai_date})")
                crossref_count = result.get("crossref_works_count")
                if crossref_count is not None:
                    crossref_date = result.get("crossref_fetched_at", "")
                    self.stdout.write(f"  Crossref: {crossref_count:,} total works (as of {crossref_date})")
                visited = result.get("visited_years", [])
                partial = result.get("partial_year")
                if visited:
                    year_parts = []
                    for y in visited:
                        label = str(y)
                        if y == partial:
                            label += " (partial — max-records hit)"
                        year_parts.append(label)
                    self.stdout.write(f"  Years:   {', '.join(year_parts)}")
            elif result["status"] == "skipped":
                self.stdout.write(self.style.WARNING(f"⊘ {result['source']}  —  skipped (disabled)"))
            elif result["status"] == "queued":
                self.stdout.write(
                    self.style.SUCCESS(
                        f"⧖ {result['source']}  —  queued (HarvestingEvent #{result.get('event_id')}, "
                        f"task id: {result.get('task_id')})"
                    )
                )
            else:
                error_msg = result.get("error", result["status"])
                dur = result.get("duration", 0)
                self.stdout.write(self.style.ERROR(f"✗ {result['source']}  —  failed ({dur:.1f}s): {error_msg}"))

        self.stdout.write(self.style.SUCCESS(f"\n{'=' * 70}"))
        if async_mode:
            queued = sum(1 for r in results if r["status"] == "queued")
            self.stdout.write(
                f"Queued {queued} harvest task(s). Watch progress via the HarvestingEvent "
                "rows printed above (open /admin/works/harvestingevent/<id>/ in the Django "
                "admin) or qmonitor; results land asynchronously."
            )
        else:
            self.stdout.write(f"Total publications harvested: {total_harvested}")
        if total_failed > 0:
            self.stdout.write(self.style.WARNING(f"Failed sources: {total_failed}"))
        if total_skipped > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"Skipped (disabled) sources: {total_skipped}. Use --include-disabled to attempt them anyway."
                )
            )
        self.stdout.write(self.style.SUCCESS(f"{'=' * 70}\n"))

    def _validate_async_coverage(
        self,
        *,
        sources_to_harvest,
        include_disabled,
        user,
        max_records,
        source_titles,
        source_issn,
        no_publisher_abstract,
        update_existing,
        full_backfill,
        since,
    ):
        """Abort --async if any explicitly-set option can't reach the task.

        Builds the task spec for each source that will actually run and checks
        that every harvest-affecting option the operator provided maps to a
        kwarg in that spec. Raises CommandError (stopping the whole command,
        before anything is enqueued) when an option cannot be matched — e.g.
        --source-title given for an OAI-PMH source, whose harvester takes no
        title filter. Keeps the async route honest as new options are added:
        wire a new flag into _build_harvest_spec or this map will flag it.
        """
        # option flag -> (was it explicitly set?, kwarg it must appear as)
        required = {
            "--max-records": (max_records is not None, "max_records"),
            "--update": (bool(update_existing), "update_existing"),
            "--user-email": (user is not None, "user"),
            "--source-title": (bool(source_titles), "source_titles"),
            "--source-issn": (bool(source_issn), "issn"),
            "--no-publisher-abstract": (bool(no_publisher_abstract), "fetch_abstract_from_publisher"),
            "--full": (bool(full_backfill), "full"),
            "--since": (bool(since), "since"),
        }
        provided = {flag: kwarg for flag, (was_set, kwarg) in required.items() if was_set}
        if not provided:
            return

        for source_key in sources_to_harvest:
            config = SOURCE_CONFIG[source_key]
            # Mirror the loop's skip logic: disabled sources that won't run
            # can't cause a coverage error.
            if not _is_enabled(config) and not include_disabled:
                continue
            source_type = config.get("source_type", "oai-pmh")
            task_path, task_kwargs = self._build_harvest_spec(
                config=config,
                source_type=source_type,
                user=user,
                max_records=max_records,
                source_titles=source_titles,
                source_issn=source_issn,
                no_publisher_abstract=no_publisher_abstract,
                update_existing=update_existing,
                full_backfill=full_backfill,
                since=since,
            )
            unmatched = [flag for flag, kwarg in provided.items() if kwarg not in task_kwargs]
            if unmatched:
                raise CommandError(
                    f"--async cannot honor {', '.join(unmatched)} for source "
                    f"'{source_key}' (type '{source_type}'): the harvest task "
                    f"{task_path} takes no such argument. Remove the option, or harvest this "
                    "source without --async (the synchronous path ignores "
                    "options that don't apply). No tasks were enqueued."
                )

    def _build_harvest_spec(
        self,
        *,
        config,
        source_type,
        user,
        max_records,
        source_titles,
        no_publisher_abstract,
        update_existing,
        full_backfill=False,
        since=None,
        source_issn=None,
    ):
        """Map a source config to its Django-Q task path and kwargs.

        Mirrors the synchronous dispatch in handle() so --async enqueues the
        same task target (the dotted paths the recurring schedules use) with
        the same arguments. Returns (dotted_path, kwargs); source.id is passed
        positionally by the caller.
        """
        common = {"user": user, "max_records": max_records, "update_existing": update_existing}

        if source_type == "rss":
            return "works.tasks.harvest_rss_endpoint", common
        if source_type == "mountain-wetlands":
            return "works.tasks.harvest_mountain_wetlands", common
        if source_type == "crossref-prefix":
            book_isbns = config.get("book_isbns")
            if book_isbns:
                return "works.tasks.harvest_crossref_book_list", {**common, "book_isbns": book_isbns}
            effective_titles = source_titles or config.get("source_titles")
            fetch_abstract = config.get("fetch_abstract_from_publisher", True) and not no_publisher_abstract
            return "works.tasks.harvest_crossref_prefix", {
                **common,
                "source_titles": effective_titles,
                "prefix": config.get("crossref_prefix"),
                "fetch_abstract_from_publisher": fetch_abstract,
                "full": full_backfill,
                "since": since,
                "issn": source_issn,
            }
        if source_type == "geoscienceworld":
            return "works.tasks.harvest_geoscienceworld", common
        if source_type == "openalex":
            return "works.tasks.harvest_openalex_source", common
        if source_type == "datacite":
            return "works.tasks.harvest_datacite", common
        if source_type == "gfz-igsn":
            return "works.tasks.harvest_gfz_igsn", common
        # Covers oai-pmh, ojs, janeway — all share the OAI harvester.
        return "works.tasks.harvest_oai_endpoint", common

    def _insert_sources(self, include_disabled=False):
        """Create Source rows for every entry in SOURCE_CONFIG without harvesting.

        Existing rows (matched by name or URL) are reported and left untouched.
        Note: Source.save() always schedules harvest_oai_endpoint, so RSS and
        Crossref-prefix sources still need the --source CLI route to harvest
        correctly — they will appear in the admin but the auto-schedule will
        not work for them until the dispatch logic is generalised.
        """
        self.stdout.write(self.style.SUCCESS(f"\n{'=' * 70}"))
        self.stdout.write(self.style.SUCCESS("Inserting sources into the database"))
        self.stdout.write(self.style.SUCCESS(f"{'=' * 70}\n"))

        created = 0
        existed = 0
        skipped = 0
        non_oai = []

        for key, config in SOURCE_CONFIG.items():
            if not _is_enabled(config) and not include_disabled:
                self.stdout.write(
                    self.style.WARNING(f"⊘ {key:15} skipped (disabled — pass --include-disabled to insert)")
                )
                if config.get("disabled_reason"):
                    self.stdout.write(f"                  Reason: {config['disabled_reason']}")
                skipped += 1
                continue

            existing = (
                Source.objects.filter(name=config["name"]).first()
                or Source.objects.filter(url_field=config["url"]).first()
            )
            if existing:
                self.stdout.write(f"= {key:15} already exists (id={existing.id}, name={existing.name!r})")
                self._reconcile_source(existing, config)
                existed += 1
                continue

            source = Source.objects.create(
                name=config["name"],
                url_field=config["url"],
                source_type=config.get("source_type", "oai-pmh"),
                collection=_get_or_create_collection(config),
                homepage_url=config.get("homepage_url"),
                publisher_name=config.get("publisher_name"),
                is_oa=config.get("is_oa", False),
                is_preprint=config.get("is_preprint", False),
                default_work_type=config.get("default_work_type", "article"),
                openalex_id=config.get("openalex_id"),
                doi_prefix=config.get("doi_prefix") or config.get("crossref_prefix"),
                issn_l=config.get("issn_l"),
                source_titles=config.get("source_titles") or None,
                harvest_interval_minutes=0,
            )
            self.stdout.write(self.style.SUCCESS(f"+ {key:15} created (id={source.id}, name={source.name!r})"))
            created += 1
            if config.get("source_type", "oai-pmh") != "oai-pmh":
                non_oai.append((key, config["source_type"]))

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Done. Created: {created}, already existed: {existed}, skipped: {skipped}.")
        )
        if non_oai:
            self.stdout.write(
                "\nNote: the following inserted sources use non-OAI source types "
                "(Source.save() dispatches to the correct task per source_type, "
                "and harvest_interval_minutes defaults to 0 so they are not auto-scheduled — "
                "run them via this management command):"
            )
            for key, source_type in non_oai:
                self.stdout.write(f"  - {key} ({source_type})")
        self.stdout.write(self.style.SUCCESS(f"\n{'=' * 70}\n"))

    def _get_or_create_source(self, config, create_if_missing):
        """Get or optionally create a Source for the given config entry."""
        source = Source.objects.filter(name=config["name"]).first()

        if not source:
            source = Source.objects.filter(url_field=config["url"]).first()

        if source:
            self.stdout.write(f"Using existing source: {source.name} (ID: {source.id})")
            self._reconcile_source(source, config)
            return source

        if not create_if_missing:
            raise CommandError(
                f"Source '{config['name']}' not found in database. Use --create-sources to automatically create it."
            )

        # Create new source
        source = Source.objects.create(
            name=config["name"],
            url_field=config["url"],
            source_type=config.get("source_type", "oai-pmh"),
            collection=_get_or_create_collection(config),
            homepage_url=config.get("homepage_url"),
            publisher_name=config.get("publisher_name"),
            is_oa=config.get("is_oa", False),
            is_preprint=config.get("is_preprint", False),
            default_work_type=config.get("default_work_type", "article"),
            openalex_id=config.get("openalex_id"),
            doi_prefix=config.get("doi_prefix") or config.get("crossref_prefix"),
            doi_contains=config.get("doi_contains", ""),
            crossref_filter=config.get("crossref_filter", ""),
            issn_l=config.get("issn_l"),
            source_titles=config.get("source_titles") or None,
            harvest_config=config.get("harvest_config") or {},
            harvest_interval_minutes=0,
        )

        self.stdout.write(self.style.SUCCESS(f"Created new source: {source.name} (ID: {source.id})"))

        return source

    def _reconcile_source(self, source, config):
        """Reconcile an existing Source row with its SOURCE_CONFIG entry.

        ``source_type`` is rewritten from the config; the other config-derived
        fields are filled only when blank so admin edits are preserved.
        """
        update_fields = []

        config_type = config.get("source_type", "oai-pmh")
        if source.source_type != config_type:
            self.stdout.write(
                self.style.WARNING(f"  Reconciled source_type: {source.source_type!r} -> {config_type!r}")
            )
            source.source_type = config_type
            update_fields.append("source_type")

        if not source.collection_id:
            col = _get_or_create_collection(config)
            if col is not None:
                self.stdout.write(f"  Linked to collection: {col.name}")
                source.collection = col
                update_fields.append("collection")

        for field in (
            "homepage_url",
            "publisher_name",
            "default_work_type",
            "openalex_id",
            "doi_prefix",
            "doi_contains",
            "crossref_filter",
            "issn_l",
            "source_titles",
            "harvest_config",
        ):
            new_value = config.get(field)
            if not new_value or getattr(source, field):
                continue
            self.stdout.write(f"  Filled blank {field}: {new_value!r}")
            setattr(source, field, new_value)
            update_fields.append(field)

        # Populate doi_prefix from crossref_prefix when not set explicitly.
        if not source.doi_prefix and config.get("crossref_prefix"):
            source.doi_prefix = config["crossref_prefix"]
            self.stdout.write(f"  Filled blank doi_prefix from crossref_prefix: {source.doi_prefix!r}")
            if "doi_prefix" not in update_fields:
                update_fields.append("doi_prefix")

        if update_fields:
            source.save(update_fields=update_fields)
        return source
