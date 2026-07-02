# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import logging

logger = logging.getLogger(__name__)

from django.contrib.auth import get_user_model
from django.contrib.gis.geos.error import GEOSException
from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver

User = get_user_model()
from django.conf import settings
from django.db.models.signals import post_save
from django.utils import timezone

User = get_user_model()
from works.models import UserProfile


@receiver(pre_save, sender=User)
def update_user_callback(sender, instance, **kwargs):
    logging.info("New user added: %s", instance.email)

    if instance.email and instance.email in settings.OPTIMAP_SUPERUSER_EMAILS and not instance.is_superuser:
        logging.warning("Registering user %s as admin", instance.email)
        instance.is_staff = True
        instance.is_superuser = True


@receiver(post_save, sender=User)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)
    else:
        # get_or_create guards against legacy/admin-created accounts that
        # were never run through the OPTIMAP login flow and have no profile.
        UserProfile.objects.get_or_create(user=instance)


@receiver(pre_delete, sender=User)
def reassign_contributions_before_user_delete(sender, instance, **kwargs):
    """Move contributions to the sentinel before a user is deleted."""
    from works.models import Contribution, CustomUser

    try:
        sentinel = CustomUser.deleted_user()
    except CustomUser.DoesNotExist:
        return
    if instance.pk == sentinel.pk:
        return
    Contribution.objects.filter(user=instance).update(user=sentinel)


# --- SEO preview-image cache invalidation (issue #22) -----------------------
# (The landing-page context cache from issue #180 is keyed on
# ``work.lastUpdate``, which Django auto-bumps to ``now()`` on every save,
# so it self-invalidates without an explicit signal.)
from works.models import Work as _Work


@receiver(post_save, sender=_Work)
def invalidate_work_preview_cache(sender, instance, **kwargs):
    """Drop the cached og:image PNG for the work whenever the work is saved.
    Lazy regeneration on the next request is cheap (a few hundred ms) and
    keeps stale geometry/title from leaking into social previews."""
    try:
        from works.services.preview_image import invalidate_preview

        invalidate_preview(instance)
    except Exception as err:  # pragma: no cover — non-critical path
        logger.debug("preview cache invalidation failed for work %s: %s", instance.pk, err)


# --- Reverse-geocoded placename (#222) + offline country assignment (#261) ---


@receiver(pre_save, sender=_Work)
def update_work_placename(sender, instance, **kwargs):
    """Populate ``placename`` via per-point reverse geocoding.

    Gated by ``OPTIMAP_GEOCODE_WORKS_ON_SAVE`` — defaults to ``True`` in
    production and is forced off under the test runner (see ``settings.py``).
    Skipped when the work has no geometry.

    Multi-point geometries are geocoded per-site and reduced to the lowest
    common ancestor in the address hierarchy — a work covering Berlin and
    Munich becomes ``"Germany"`` rather than the misleading
    centroid-of-Berlin-and-Munich result. See
    ``works.services.geocoding.geocode_geometry``.

    Country association is *not* set here: it lives in the ``Work.countries``
    M2M, populated by the offline point-in-polygon join in
    ``assign_work_countries`` (post-save) — see issue #261.

    On a complete geocoding failure (no point returned an address — typically
    a transient Nominatim outage) we leave the existing fields untouched
    rather than blank a previously-populated placename. A real "geometry
    spans incompatible regions" result (some addresses, no shared LCA)
    *does* clear the fields — that's the honest representation.
    """
    if not getattr(settings, "GEOCODE_WORKS_ON_SAVE", False):
        return
    # Respect an authoritative placename supplied by the harvester (e.g. an IGSN
    # sample's DataCite geoLocationPlace) — don't overwrite it with reverse
    # geocoding. Marked via provenance.metadata_sources.placename (#187).
    prov = instance.provenance if isinstance(instance.provenance, dict) else {}
    if instance.placename and (prov.get("metadata_sources") or {}).get("placename"):
        return
    geom = instance.geometry
    if not geom or geom.empty:
        return
    try:
        from works.services.geocoding import (
            collect_geocoding_matches,
            geocode_geometry,
        )

        placename, _country_code, n_geocoded = geocode_geometry(geom)
    except Exception as err:  # pragma: no cover — geocode_geometry never raises
        logger.warning("reverse-geocode failed for work %s: %s", instance.pk, err)
        return
    if n_geocoded == 0:
        # All Nominatim calls failed — preserve any populated fields, and
        # leave any existing geocoding-provenance block untouched (it still
        # describes the values currently in the DB).
        return
    instance.placename = placename

    # Per-point matches (cache hit since geocode_geometry just populated it).
    # For multi-point geometries the LCA placename is broader than any single
    # match, so the honest representation is the list of underlying matches
    # that produced it — each one with its OSM identifiers.
    try:
        matches = collect_geocoding_matches(geom)
    except Exception as err:  # pragma: no cover — defensive
        logger.warning("collect_geocoding_matches failed for work %s: %s", instance.pk, err)
        matches = []

    # Record reverse-geocoding provenance — single block, overwritten on each
    # successful run, so it always describes the current placename.
    provenance = instance.provenance if isinstance(instance.provenance, dict) else {}
    provenance["geocoding"] = {
        "gazetteer": "Nominatim",
        "gazetteer_url": "https://nominatim.openstreetmap.org/",
        "placename": placename,
        "n_geocoded": n_geocoded,
        "matches": matches,
        "geocoded_at": timezone.now().isoformat(),
    }
    instance.provenance = provenance


@receiver(pre_save, sender=_Work)
def track_work_geometry_change(sender, instance, **kwargs):
    """Flag whether the geometry changed, for ``assign_work_countries`` (#261).

    A curator's manual country decision is only valid for the geometry it was
    made against; when the geometry changes the decision is void and the work
    re-enters automated matching. New works (no PK) count as changed.
    """
    if not instance.pk:
        instance._geometry_changed = True
        return
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "geometry" not in update_fields:
        # A targeted save that doesn't touch geometry can't void a country
        # decision — skip the extra SELECT + WKB parse.
        instance._geometry_changed = False
        return
    old = sender.objects.filter(pk=instance.pk).values_list("geometry", flat=True).first()
    new = instance.geometry
    if (old is None) != (new is None):
        instance._geometry_changed = True
    elif old is None:  # both None
        instance._geometry_changed = False
    else:
        try:
            instance._geometry_changed = not old.equals(new)
        except GEOSException as err:
            # An invalid geometry (e.g. a self-intersecting polygon) makes GEOS
            # refuse to evaluate the equality predicate. Don't let that abort the
            # save; treat it as changed so automated country matching re-runs.
            logger.warning(
                "geometry equality check failed for work %s (%s); treating as changed",
                instance.pk,
                err,
            )
            instance._geometry_changed = True


@receiver(post_save, sender=_Work)
def assign_work_countries(sender, instance, **kwargs):
    """Populate ``Work.countries`` via an offline point-in-polygon join (#261).

    Runs post-save (M2M needs a PK). Gated by ``OPTIMAP_GEOCODE_WORKS_ON_SAVE``
    for parity with the Nominatim placename signal — the recurring sweep
    (``works.tasks.backfill_work_countries``) is the guaranteed catch-up path
    for works saved with it off, during a load before ``load_countries``, or
    legacy records. Naturally multi-valued: a transboundary geometry links
    every intersecting country. ``set()`` does not re-fire ``Work`` save, so
    there is no recursion.

    A manual curation decision (``provenance["countries"]["source"] ==
    "manual"`` — a curator-assigned country or a "will not be matched"
    exclusion) is preserved across unrelated saves and only voided when the
    geometry changes, at which point the automated join below overwrites it.
    """
    if not getattr(settings, "GEOCODE_WORKS_ON_SAVE", False):
        return
    geom = instance.geometry
    if not geom or geom.empty:
        return
    provenance = instance.provenance if isinstance(instance.provenance, dict) else {}
    is_manual = provenance.get("countries", {}).get("source") == "manual"
    if is_manual and not getattr(instance, "_geometry_changed", True):
        return
    try:
        from works.services.countries import lookup_countries
        from works.utils.provenance import set_block

        countries, prov = lookup_countries(geom)
        instance.countries.set(countries)
        if prov is not None:
            set_block(instance, "countries", prov)
        elif is_manual:
            # Geometry changed to one that matches no country: drop the now-void
            # manual block so the work isn't still flagged as a manual decision.
            set_block(instance, "countries", None)
    except Exception as err:  # pragma: no cover — non-critical path
        logger.warning("country assignment failed for work %s: %s", instance.pk, err)


@receiver(post_save, sender=_Work)
def assign_work_regions(sender, instance, **kwargs):
    """Populate ``Work.regions`` via an offline point-in-polygon join.

    The global-region mirror of :func:`assign_work_countries`: runs post-save
    (M2M needs a PK), gated by ``OPTIMAP_GEOCODE_WORKS_ON_SAVE``, with the
    recurring sweep (``works.tasks.backfill_work_regions``) as the guaranteed
    catch-up path for works saved with it off, before ``load_global_regions``
    ran, or legacy records. Naturally multi-valued (a coastal work links its
    continent and ocean). ``set()`` does not re-fire ``Work`` save, so there is
    no recursion.

    A manual curation decision (``provenance["regions"]["source"] == "manual"``
    — a curator-assigned region or a "will not be matched" exclusion, set on the
    ``/regions/`` page) is preserved across unrelated saves and only voided when
    the geometry changes, mirroring :func:`assign_work_countries`. Otherwise the
    join re-runs on every save and overwrites, which keeps it self-healing (a
    work that matched nothing because ``GlobalRegion`` was empty at save time is
    re-linked on its next save).
    """
    if not getattr(settings, "GEOCODE_WORKS_ON_SAVE", False):
        return
    geom = instance.geometry
    if not geom or geom.empty:
        return
    provenance = instance.provenance if isinstance(instance.provenance, dict) else {}
    is_manual = provenance.get("regions", {}).get("source") == "manual"
    if is_manual and not getattr(instance, "_geometry_changed", True):
        return
    try:
        from works.services.regions import lookup_regions
        from works.utils.provenance import set_block
        from works.views_regions import invalidate_region_page_cache

        regions, prov = lookup_regions(geom)
        previous = set(instance.regions.all())
        instance.regions.set(regions)
        if prov is not None:
            set_block(instance, "regions", prov)
        elif is_manual:
            # Geometry changed to one matching no region: drop the now-void manual
            # block so the work re-enters automated matching / the curation list.
            set_block(instance, "regions", None)
        for region in set(regions) | previous:
            invalidate_region_page_cache(region)
    except Exception as err:  # pragma: no cover — non-critical path
        logger.warning("region assignment failed for work %s: %s", instance.pk, err)


# --- Generic work↔work relationships (#187) ---


@receiver(post_save, sender=_Work)
def resolve_incoming_work_relations(sender, instance, **kwargs):
    """Connect dangling ``WorkRelation`` rows that point at this work's identifier.

    ``WorkRelation`` rows harvested from DataCite ``relatedIdentifiers`` start
    out identifier-only (``to_work`` null) because the related resource is
    usually not yet in OPTIMAP. When that resource is later harvested, this
    signal upgrades every dangling relation whose ``to_identifier`` matches the
    new work's DOI or URL to a concrete ``to_work`` FK. ``to_identifier`` is
    kept for provenance. ``update()`` does not re-fire ``Work`` save, so there
    is no recursion.
    """
    from works.models import WorkRelation

    candidates = [v for v in (instance.doi, instance.url) if v]
    if not candidates:
        return
    try:
        WorkRelation.objects.filter(to_work__isnull=True, to_identifier__in=candidates).update(to_work=instance)
    except Exception as err:  # pragma: no cover — non-critical path
        logger.warning("work-relation resolution failed for work %s: %s", instance.pk, err)
