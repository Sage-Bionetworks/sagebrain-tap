"""Unit tests for the pure logic: identifiers, vocabularies and label handling.

Covers the cases where a silent wrong answer is possible -- a guessed IRI, a
dropped vocabulary value, an ambiguous label resolved by accident -- rather than
re-testing what the acceptance checks already verify against a loaded graph.
"""

import unittest

from opentargets import common
from opentargets.export_label_index import KIND_RANK
from opentargets.transform_molecules import labels_of


class DiseaseIdentifiers(unittest.TestCase):
    def test_efo_and_obo_terms_take_different_bases(self):
        self.assertEqual(common.expand(common.disease_curie("EFO_0000658")),
                         "http://www.ebi.ac.uk/efo/EFO_0000658")
        self.assertEqual(common.expand(common.disease_curie("MONDO_0017827")),
                         "http://purl.obolibrary.org/obo/MONDO_0017827")

    def test_orphanet_takes_ordo(self):
        self.assertEqual(common.expand(common.disease_curie("Orphanet_363")),
                         "http://www.orpha.net/ORDO/Orphanet_363")

    def test_unknown_prefix_raises_rather_than_guessing(self):
        """A guessed IRI would join to nothing and read as absent data."""
        with self.assertRaises(common.IngestError):
            common.disease_curie("NEWONTO_0001")

    def test_id_without_underscore_raises(self):
        with self.assertRaises(common.IngestError):
            common.disease_curie("justastring")

    def test_phenotype_terms_are_not_typed_as_diseases(self):
        self.assertEqual(common.disease_node_class("MONDO_0017827"), "biolink:Disease")
        self.assertEqual(common.disease_node_class("HP_0001234"),
                         "biolink:PhenotypicFeature")
        self.assertEqual(common.disease_node_class("MP_0001234"),
                         "biolink:PhenotypicFeature")
        self.assertEqual(common.disease_node_class("GO_0001234"),
                         "biolink:DiseaseOrPhenotypicFeature")


class CompoundAndGeneIdentifiers(unittest.TestCase):
    def test_chembl_keeps_its_prefix_in_the_local_part(self):
        """identifiers.org keys ChEMBL on the full accession, unlike HGNC."""
        self.assertEqual(common.expand(common.chembl_curie("CHEMBL2103875")),
                         "https://identifiers.org/chembl:CHEMBL2103875")

    def test_non_chembl_identifier_raises(self):
        with self.assertRaises(common.IngestError):
            common.chembl_curie("DB00398")

    def test_hgnc_keys_on_the_number(self):
        self.assertEqual(common.expand(common.hgnc_curie("HGNC:6840")),
                         "https://identifiers.org/hgnc:6840")
        self.assertEqual(common.hgnc_curie("6840"), "HGNC:6840")


class Vocabularies(unittest.TestCase):
    def test_unknown_value_raises_and_names_the_constant(self):
        with self.assertRaises(common.IngestError) as caught:
            common.check_vocabulary("SUPERINHIBITOR", common.ACTION_TYPES,
                                    "actionType", "ACTION_TYPES")
        self.assertIn("ACTION_TYPES", str(caught.exception))

    def test_stages_are_ordered_weakest_to_strongest(self):
        self.assertLess(common.stage_rank("PHASE_1"), common.stage_rank("PHASE_3"))
        self.assertLess(common.stage_rank("PHASE_3"), common.stage_rank("APPROVAL"))
        self.assertEqual(common.stage_rank("UNKNOWN"), 0,
                         "UNKNOWN means no phase recorded, not an early one")

    def test_unknown_stage_raises(self):
        with self.assertRaises(common.IngestError):
            common.stage_rank("PHASE_4")

    def test_group_target_types_are_not_single_targets(self):
        """The distinction a consumer must filter on to avoid over-counting."""
        self.assertIn("single protein", common.SINGLE_TARGET_TYPES)
        for group in ("protein family", "protein complex", "selectivity group"):
            self.assertIn(group, common.TARGET_TYPES)
            self.assertNotIn(group, common.SINGLE_TARGET_TYPES)

    def test_only_small_molecules_get_the_narrow_class(self):
        self.assertEqual(common.drug_type_class("Small molecule"),
                         "biolink:SmallMolecule")
        self.assertEqual(common.drug_type_class("Antibody"), "biolink:ChemicalEntity")


class Labels(unittest.TestCase):
    def test_preferred_name_synonyms_and_trade_names_are_distinguished(self):
        row = {
            "name": "SELUMETINIB",
            "synonyms": [{"label": "AZD-6244", "source": "ChEMBL"}],
            "tradeNames": [{"label": "KOSELUGO", "source": "ChEMBL"}],
        }
        self.assertEqual(labels_of(row), [
            ("SELUMETINIB", "preferred_name", "open-targets"),
            ("AZD-6244", "synonym", "ChEMBL"),
            ("KOSELUGO", "trade_name", "ChEMBL"),
        ])

    def test_blank_and_missing_label_lists_are_skipped(self):
        self.assertEqual(labels_of({"name": "", "synonyms": None, "tradeNames": []}), [])
        self.assertEqual(labels_of({"name": "X", "synonyms": [{"label": "  "}]}),
                         [("X", "preferred_name", "open-targets")])

    def test_preferred_name_outranks_synonym(self):
        """One row per candidate keeps a consumer's ambiguity count honest."""
        self.assertLess(KIND_RANK["preferred_name"], KIND_RANK["synonym"])
        self.assertLess(KIND_RANK["synonym"], KIND_RANK["trade_name"])


class ReleaseGraph(unittest.TestCase):
    def test_graph_uri_is_per_release(self):
        self.assertEqual(common.release_graph("26.06"),
                         "urn:sagebrain:opentargets:26.06")
        self.assertNotEqual(common.release_graph("26.06"),
                            common.release_graph("26.09"))


if __name__ == "__main__":
    unittest.main()
