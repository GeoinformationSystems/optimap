# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Author identity linking for ``Work`` (issue #29 /by/ facet).

Associates a work with :class:`works.models.Author` rows by upserting identity-
bearing author records (those carrying an ORCID or OpenAlex author id) from
enrichment data and writing :class:`works.models.WorkAuthor` through-rows.

The existing ``Work.authors`` ArrayField of display names is the authoritative
display source and is never modified here. This module only populates the
relational M2M so that ``/by/<orcid>`` queries work.
"""

import logging

from django.db import transaction
from django.urls import reverse

logger = logging.getLogger(__name__)


def link_work_authors(work, records, *, source: str, force: bool = False, supplement: bool = False) -> int:
    """Upsert Author rows and create WorkAuthor links for ``work``.

    ``records`` is a list of dicts, each with any subset of:
        {"name": str, "orcid": str | None, "openalex_id": str | None, "rank": int}

    Only records with at least one identifier (orcid or openalex_id) produce an
    Author row. Records with neither are silently skipped.

    Fill-if-empty policy: if the work already has author_links and ``force`` is
    False, returns 0 without touching the database. Pass ``force=True`` to
    rebuild (used by the backfill command).

    Pass ``supplement=True`` to run a second-source merge pass instead of
    fill-if-empty. Supplement mode adds ORCIDs not yet linked, and records
    any rank-position conflicts (same rank, different ORCID) in provenance
    without overwriting the existing link. It never deletes existing rows.

    Returns the number of WorkAuthor rows created.
    """
    from works.models import Author, WorkAuthor
    from works.utils import provenance as prov

    if not records:
        return 0

    if supplement:
        return _supplement_authors(work, records, source=source)

    if not force and work.author_links.exists():
        return 0

    linked = 0
    orcids_linked = []

    with transaction.atomic():
        if force:
            WorkAuthor.objects.filter(work=work).delete()

        for record in records:
            orcid = record.get("orcid") or None
            openalex_id = record.get("openalex_id") or None
            name = (record.get("name") or "").strip()
            rank = record.get("rank", 0)

            if not orcid and not openalex_id:
                continue

            # Upsert: prefer ORCID as the stable key; fall back to openalex_id.
            author = None
            if orcid:
                author, _ = Author.objects.get_or_create(
                    orcid=orcid,
                    defaults={"name": name or "Unknown", "openalex_id": openalex_id},
                )
                # Update name and openalex_id if we have better data.
                changed = False
                if name and author.name != name:
                    author.name = name
                    changed = True
                if openalex_id and author.openalex_id != openalex_id:
                    author.openalex_id = openalex_id
                    changed = True
                if changed:
                    author.save(update_fields=["name", "openalex_id"])
            elif openalex_id:
                author, _ = Author.objects.get_or_create(
                    openalex_id=openalex_id,
                    defaults={"name": name or "Unknown"},
                )
                if name and author.name != name:
                    author.name = name
                    author.save(update_fields=["name"])

            if author is None:
                continue

            WorkAuthor.objects.get_or_create(work=work, author=author, defaults={"rank": rank})
            linked += 1
            if author.orcid:
                orcids_linked.append(author.orcid)

    if linked:
        # Record provenance: metadata_sources key + event.
        p = prov._ensure_dict(work.provenance)
        p.setdefault("metadata_sources", {})["author_orcids"] = source
        prov.append_event(work, "author_link", source=source, linked=linked, orcids=orcids_linked)
        # Persist provenance (append_event mutates work.provenance in memory; flush with update).
        type(work).objects.filter(pk=work.pk).update(provenance=work.provenance)

    return linked


def _supplement_authors(work, records, *, source: str) -> int:
    """Second-source merge pass: add ORCIDs not yet linked, document conflicts.

    Called by :func:`link_work_authors` when ``supplement=True``. Only processes
    ORCID-bearing records. For each:

    - Already linked ORCID → counted as consistent, no write.
    - Different ORCID at the same rank position → conflict recorded in provenance,
      existing link kept (first-source wins).
    - New ORCID not yet linked → Author upserted and WorkAuthor created.

    Provenance is updated even when nothing is added, so conflicts are always
    visible. The ``metadata_sources["author_orcids"]`` value is extended with
    ``+<source>`` when at least one author is added.
    """
    from works.models import Author, WorkAuthor
    from works.utils import provenance as prov

    existing_wa = list(WorkAuthor.objects.filter(work=work).select_related("author"))
    linked_orcids = {wa.author.orcid for wa in existing_wa if wa.author.orcid}
    existing_by_rank = {wa.rank: wa for wa in existing_wa}

    added = 0
    consistent = 0
    conflicts = []
    orcids_added = []

    with transaction.atomic():
        for record in records:
            orcid = record.get("orcid") or None
            if not orcid:
                continue

            rank = record.get("rank", 0)
            name = (record.get("name") or "").strip()

            if orcid in linked_orcids:
                consistent += 1
                continue

            existing_at_rank = existing_by_rank.get(rank)
            if existing_at_rank and existing_at_rank.author.orcid and existing_at_rank.author.orcid != orcid:
                conflicts.append(
                    {
                        "rank": rank,
                        "kept": existing_at_rank.author.orcid,
                        "from_source": orcid,
                    }
                )
                continue

            author, _ = Author.objects.get_or_create(
                orcid=orcid,
                defaults={"name": name or "Unknown"},
            )
            if name and author.name != name:
                author.name = name
                author.save(update_fields=["name"])
            WorkAuthor.objects.get_or_create(work=work, author=author, defaults={"rank": rank})
            linked_orcids.add(orcid)
            added += 1
            orcids_added.append(orcid)

    if added or consistent or conflicts:
        p = prov._ensure_dict(work.provenance)
        if added:
            ms = p.setdefault("metadata_sources", {})
            existing_val = ms.get("author_orcids", "")
            if source not in existing_val:
                ms["author_orcids"] = f"{existing_val}+{source}" if existing_val else source
        event_fields = {"added": added, "consistent": consistent, "source": source}
        if conflicts:
            event_fields["conflicts"] = conflicts
        prov.append_event(work, "author_link_supplement", **event_fields)
        type(work).objects.filter(pk=work.pk).update(provenance=work.provenance)

    return added


def authors_for_display(work) -> list:
    """Return a list of dicts for rendering the author list with optional ORCID links.

    Each dict: {"name": str, "orcid": str | None, "by_url": str | None}

    When WorkAuthor rows exist and cover at least as many authors as the
    ``Work.authors`` display cache, they are used as the primary source of both
    names and ORCID links — their names come from enrichment (OpenAlex /
    OpenAIRE) and are more reliable than the harvested display cache. This fixes
    cases where the harvester stored split name parts (e.g. ``["Galasso",
    "Gabriele"]``) instead of one full-name entry per author.

    When WorkAuthor covers *fewer* authors than the display cache (some co-authors
    have no identity record), the function falls back to aligning ``Work.authors``
    names against WorkAuthor by rank/index and overlays ORCID links where
    available.

    Authors without an ORCID get ``orcid=None, by_url=None`` and are rendered as
    plain text — no links, no fallback page.
    """
    names = work.authors or []

    try:
        wa_qs = work.work_authors.select_related("author").order_by("rank")
        ranked = [(wa.rank, wa.author) for wa in wa_qs]
    except Exception:
        ranked = []

    if not ranked:
        return [{"name": name, "orcid": None, "by_url": None} for name in names]

    # WorkAuthor rows cover all (or more) authors → prefer their names.
    if len(ranked) >= len(names):
        result = []
        for _, author in ranked:
            if author.orcid:
                by_url = reverse("optimap:by-author", kwargs={"orcid": author.orcid})
                result.append({"name": author.name, "orcid": author.orcid, "by_url": by_url})
            else:
                result.append({"name": author.name, "orcid": None, "by_url": None})
        return result

    # Fewer WorkAuthor rows than display names: align by rank/index and overlay ORCID.
    # Use Author.name for linked authors (enriched full name) rather than the display
    # cache entry, which may be a bare surname or differently formatted.
    orcid_by_index = {rank: author for rank, author in ranked if rank < len(names)}
    name_map = {a.name.lower(): a for _, a in ranked}
    result = []
    for i, name in enumerate(names):
        author = orcid_by_index.get(i) or name_map.get(name.lower())
        if author and author.orcid:
            by_url = reverse("optimap:by-author", kwargs={"orcid": author.orcid})
            result.append({"name": author.name or name, "orcid": author.orcid, "by_url": by_url})
        else:
            result.append({"name": name, "orcid": None, "by_url": None})
    return result
