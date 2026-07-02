# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the generic WorkRelation model + dangling-link resolver (#187)."""

from django.db import IntegrityError
from django.test import TestCase

from works.models import Work, WorkRelation


class WorkRelationModelTest(TestCase):
    def setUp(self):
        self.sample = Work.objects.create(title="IGSN sample", type="physical-object", status="p")

    def test_normalize_relation_type(self):
        # DataCite vocabulary maps to our canonical set (case-insensitive).
        self.assertEqual(WorkRelation.normalize_relation_type("IsCitedBy"), "is_cited_by")
        self.assertEqual(WorkRelation.normalize_relation_type("isreferencedby"), "is_referenced_by")
        self.assertEqual(WorkRelation.normalize_relation_type("IsSupplementTo"), "is_supplement_to")
        # Unknown / empty falls back to the generic relation.
        self.assertEqual(WorkRelation.normalize_relation_type("HasWeirdRelation"), "related_to")
        self.assertEqual(WorkRelation.normalize_relation_type(""), "related_to")
        self.assertEqual(WorkRelation.normalize_relation_type(None), "related_to")

    def test_dangling_relation_str(self):
        rel = WorkRelation.objects.create(
            from_work=self.sample,
            to_identifier="10.15485/3374642",
            to_identifier_type="DOI",
            relation_type="is_referenced_by",
        )
        self.assertIsNone(rel.to_work_id)
        self.assertIn("is_referenced_by", str(rel))
        self.assertIn("10.15485/3374642", str(rel))

    def test_uniqueness_constraint(self):
        WorkRelation.objects.create(
            from_work=self.sample,
            to_identifier="10.15485/3374642",
            to_identifier_type="DOI",
            relation_type="is_referenced_by",
        )
        with self.assertRaises(IntegrityError):
            WorkRelation.objects.create(
                from_work=self.sample,
                to_identifier="10.15485/3374642",
                to_identifier_type="DOI",
                relation_type="is_referenced_by",
            )


class WorkRelationResolverTest(TestCase):
    """The post_save signal upgrades dangling relations to a to_work FK."""

    def setUp(self):
        self.sample = Work.objects.create(title="IGSN sample", type="physical-object", status="p")

    def test_resolves_by_doi_when_target_harvested_later(self):
        rel = WorkRelation.objects.create(
            from_work=self.sample,
            to_identifier="10.15485/3374642",
            to_identifier_type="DOI",
            relation_type="is_referenced_by",
        )
        self.assertIsNone(rel.to_work_id)

        # The referenced publication is harvested afterwards.
        paper = Work.objects.create(title="Referencing paper", doi="10.15485/3374642", status="p")

        rel.refresh_from_db()
        self.assertEqual(rel.to_work_id, paper.id)
        # to_identifier is kept for provenance even after resolution.
        self.assertEqual(rel.to_identifier, "10.15485/3374642")

    def test_resolves_by_url(self):
        rel = WorkRelation.objects.create(
            from_work=self.sample,
            to_identifier="https://example.org/dataset/1",
            to_identifier_type="URL",
            relation_type="references",
        )
        target = Work.objects.create(title="Target", url="https://example.org/dataset/1", status="p")
        rel.refresh_from_db()
        self.assertEqual(rel.to_work_id, target.id)

    def test_unrelated_save_does_not_resolve(self):
        rel = WorkRelation.objects.create(
            from_work=self.sample,
            to_identifier="10.15485/3374642",
            to_identifier_type="DOI",
            relation_type="is_referenced_by",
        )
        Work.objects.create(title="Different work", doi="10.9999/other", status="p")
        rel.refresh_from_db()
        self.assertIsNone(rel.to_work_id)


class WorkSerializerRelationsTest(TestCase):
    """related_works exposure on the full serializer + type on the minimal one."""

    def test_minimal_serializer_includes_type(self):
        from works.serializers import WorkMinimalSerializer

        sample = Work.objects.create(title="s", type="physical-object", status="p")
        data = WorkMinimalSerializer(sample).data
        self.assertEqual(data["properties"]["type"], "physical-object")

    def test_related_works_field(self):
        from works.serializers import WorkSerializer

        sample = Work.objects.create(title="s", type="physical-object", status="p")
        paper = Work.objects.create(title="p", doi="10.1/paper", status="p")
        WorkRelation.objects.create(
            from_work=sample,
            to_work=paper,
            to_identifier="10.1/paper",
            to_identifier_type="DOI",
            relation_type="is_referenced_by",
        )
        WorkRelation.objects.create(
            from_work=sample,
            to_identifier="https://example.org/x",
            to_identifier_type="URL",
            relation_type="references",
        )
        related = WorkSerializer(sample).data["properties"]["related_works"]
        self.assertEqual(len(related), 2)
        resolved = next(r for r in related if r["identifier_type"] == "DOI")
        self.assertEqual(resolved["work_id"], paper.id)
        self.assertEqual(resolved["work_doi"], "10.1/paper")
        dangling = next(r for r in related if r["identifier_type"] == "URL")
        self.assertIsNone(dangling["work_id"])


class WorkTypeIconTest(TestCase):
    def test_icon_mapping(self):
        from works.templatetags.optimap_extras import work_type_icon

        self.assertEqual(work_type_icon("physical-object"), "fas fa-vial")
        self.assertEqual(work_type_icon("dataset"), "fas fa-database")
        self.assertEqual(work_type_icon("article"), "fas fa-file-lines")
        # Accepts a Work instance too.
        sample = Work.objects.create(title="s", type="physical-object", status="p")
        self.assertEqual(work_type_icon(sample), "fas fa-vial")


class PlacenameSourceTipTest(TestCase):
    def test_tip_is_provenance_specific(self):
        from works.templatetags.optimap_extras import placename_source_tip

        # IGSN sample with a DataCite-supplied place.
        sample = Work.objects.create(
            title="s",
            type="physical-object",
            status="p",
            placename="Oak Ridge",
            provenance={"metadata_sources": {"placename": "datacite.geoLocationPlace"}},
        )
        self.assertIn("DataCite", placename_source_tip(sample))
        self.assertNotIn("Nominatim", placename_source_tip(sample))

        # GFZ landing-page place.
        gfz = Work.objects.create(
            title="g",
            type="physical-object",
            status="p",
            placename="Potsdam",
            provenance={"metadata_sources": {"placename": "gfz.landing_page"}},
        )
        self.assertIn("GFZ", placename_source_tip(gfz))

        # Everything else falls back to the Nominatim message.
        paper = Work.objects.create(title="p", status="p", placename="Berlin")
        self.assertIn("Nominatim", placename_source_tip(paper))
