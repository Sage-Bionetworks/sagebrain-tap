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
            common.stage_rank("PHASE_5")

    def test_phase_4_outranks_approval(self):
        """Phase-4 studies are post-marketing, so they sit above approval."""
        self.assertGreater(common.stage_rank("PHASE_4"),
                           common.stage_rank("APPROVAL"))

    def test_withdrawal_is_valid_but_unrankable(self):
        """A withdrawn drug reached approval then was pulled. Ranking it either
        way asserts something false, so ranking refuses rather than guesses."""
        self.assertIn("WITHDRAWAL", common.CLINICAL_STAGES)
        self.assertNotIn("WITHDRAWAL", common.CLINICAL_STAGE_RANK)
        with self.assertRaises(common.IngestError) as caught:
            common.stage_rank("WITHDRAWAL")
        self.assertIn("CLINICAL_STAGE_ORDER", str(caught.exception))

    def test_report_only_stages_are_still_enforced(self):
        """No trial carries either value at 26.06, yet both stay enforced:
        PHASE_4 is on 28,465 trial nodes and WITHDRAWAL on none, and a set that
        tracked only what a release happens to use would fail on the next one."""
        for stage in ("PHASE_4", "WITHDRAWAL"):
            common.check_vocabulary(stage, common.CLINICAL_STAGES,
                                    "clinicalStage", "CLINICAL_STAGES")

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


class TrialIdentifiers(unittest.TestCase):
    def test_lowercase_source_id_becomes_a_registry_accession(self):
        """The source spells it nct02407405; every registry spells it NCT02407405."""
        self.assertEqual(common.trial_curie("nct02407405"),
                         "CLINICALTRIALS:NCT02407405")
        self.assertEqual(common.expand(common.trial_curie("nct02407405")),
                         "https://identifiers.org/clinicaltrials:NCT02407405")

    def test_non_trial_report_ids_raise(self):
        """The three unprojected record kinds key on anything at all, which is
        exactly why they are out of scope. Any of them reaching trial_curie means
        the scope filter leaked, so it fails rather than minting a dead IRI."""
        for report_id in ("019909s020lbl.pdf", "a01ab02",
                          "https://www.fda.gov/some/label", "nct1234567",
                          "NCT123456789", ""):
            with self.assertRaises(common.IngestError, msg=report_id):
                common.trial_curie(report_id)

    def test_uppercase_input_is_accepted_unchanged(self):
        self.assertEqual(common.trial_curie("NCT02407405"),
                         "CLINICALTRIALS:NCT02407405")


class TrialStartWindow(unittest.TestCase):
    def test_window_runs_from_1950_to_ten_years_past_the_release(self):
        self.assertEqual(common.trial_start_window("26.06"), (1950, 2036))
        self.assertEqual(common.trial_start_window("30.12"), (1950, 2040))

    def test_window_clears_planned_starts_but_catches_placeholders(self):
        """Range and precision are separate questions; this is the range one.
        150 trials start in 2027-2030 and are real; 2099-01-01 is not."""
        earliest, latest = common.trial_start_window("26.06")
        for year in (1962, 1999, 2026, 2030):
            self.assertTrue(earliest <= year <= latest, year)
        for year in (1900, 1931, 2040, 2099):
            self.assertFalse(earliest <= year <= latest, year)

    def test_malformed_release_raises_rather_than_guessing_a_year(self):
        for release in ("2026.06", "26", "v26.06", "26.6"):
            with self.assertRaises(common.IngestError, msg=release):
                common.release_year(release)


class TrialVocabularies(unittest.TestCase):
    def test_only_clinical_trials_are_projected(self):
        self.assertIn(common.TRIAL_RECORD_TYPE, common.REPORT_TYPES)
        self.assertEqual(len(common.REPORT_TYPES), 4)

    def test_source_own_unclassified_values_are_members_not_fallbacks(self):
        """Uncategorised and No_Context are values the release assigns. Treating
        either as a default would hide a genuinely new category behind it."""
        for value in ("Uncategorised", "No_Context", "Invalid_Reason"):
            common.check_vocabulary(value, common.TRIAL_STOP_REASON_CATEGORIES,
                                    "trialStopReasonCategories",
                                    "TRIAL_STOP_REASON_CATEGORIES")
        with self.assertRaises(common.IngestError) as caught:
            common.check_vocabulary("Funding_Withdrawn",
                                    common.TRIAL_STOP_REASON_CATEGORIES,
                                    "trialStopReasonCategories",
                                    "TRIAL_STOP_REASON_CATEGORIES")
        self.assertIn("TRIAL_STOP_REASON_CATEGORIES", str(caught.exception))

    def test_all_four_quality_controls_are_emitted_not_just_the_gating_pair(self):
        """Emitting only the gating flags would let a consumer reproduce the
        release's indication filter but never relax it."""
        self.assertTrue(common.INDICATION_GATING_QUALITY_CONTROLS
                        < common.REPORT_QUALITY_CONTROLS)
        self.assertEqual(len(common.REPORT_QUALITY_CONTROLS), 4)
        self.assertEqual(common.INDICATION_GATING_QUALITY_CONTROLS,
                         {"PHASE_IV_NOT_APPROVED", "INDIRECT_PRIMARY_PURPOSE"})

    def test_clinical_report_is_projected_and_gates_what_trials_read(self):
        spec = common.DATASETS["clinical_report"]
        self.assertTrue(spec.projected)
        for column in ("trialOfficialTitle", "trialOverallStatus",
                       "qualityControls", "trialStopReasonCategories",
                       "trialStartDate"):
            self.assertIn(column, spec.required_columns)

    def test_statuses_that_can_carry_a_stop_reason_are_a_strict_subset(self):
        self.assertTrue(common.TRIAL_STOPPED_STATUSES
                        < common.TRIAL_OVERALL_STATUSES)
        self.assertEqual(len(common.TRIAL_OVERALL_STATUSES), 13)

    def test_withdrawn_trial_and_withdrawal_stage_are_unrelated_facts(self):
        """Near-identical spellings on different columns and different subjects:
        a trial that never enrolled anyone, versus a drug pulled after approval."""
        self.assertIn("WITHDRAWN", common.TRIAL_OVERALL_STATUSES)
        self.assertNotIn("WITHDRAWAL", common.TRIAL_OVERALL_STATUSES)
        self.assertIn("WITHDRAWAL", common.CLINICAL_STAGES)
        self.assertNotIn("WITHDRAWN", common.CLINICAL_STAGES)


class Labels(unittest.TestCase):
    def test_preferred_name_synonyms_and_trade_names_are_distinguished(self):
        row = {
            "name": "SELUMETINIB",
            "synonyms": [{"label": "AZD-6244", "source": "ChEMBL"}],
            "tradeNames": [{"label": "KOSELUGO", "source": "ChEMBL"}],
        }
        self.assertEqual(labels_of(row)[0], [
            ("SELUMETINIB", "preferred_name", "open-targets"),
            ("AZD-6244", "synonym", "ChEMBL"),
            ("KOSELUGO", "trade_name", "ChEMBL"),
        ])

    def test_blank_and_missing_label_lists_are_skipped(self):
        self.assertEqual(labels_of({"name": "", "synonyms": None, "tradeNames": []}),
                         ([], []))
        self.assertEqual(labels_of({"name": "X", "synonyms": [{"label": "  "}]})[0],
                         [("X", "preferred_name", "open-targets")])

    def test_a_label_with_a_control_character_is_rejected_not_cleaned(self):
        """Three exist at 26.06, all upstream mojibake of a trademark sign.
        Stripping the NUL from 'verorab\x00ae' yields 'verorabae' -- two
        fragments fused into a word no source wrote, which would match nothing
        and, if it ever matched, would match falsely."""
        usable, rejected = labels_of({
            "name": "VERORAB",
            "synonyms": [{"label": "verorab\x00ae", "source": "ChEMBL"},
                         {"label": "rabies vaccine", "source": "ChEMBL"}],
        })
        self.assertEqual(usable, [("VERORAB", "preferred_name", "open-targets"),
                                  ("rabies vaccine", "synonym", "ChEMBL")])
        self.assertEqual(rejected, [("verorab\x00ae", "synonym", "ChEMBL")])

    def test_tab_and_newline_are_not_control_characters_here(self):
        """TurtleWriter escapes those properly, so they are safe to emit."""
        self.assertFalse(common.has_control_characters("a\tb\nc\r"))
        for bad in ("\x00", "\x01", "\x1f", "\x7f", "\x9f"):
            self.assertTrue(common.has_control_characters(f"x{bad}y"), repr(bad))

    def test_literal_refuses_control_characters_as_a_backstop(self):
        """Turtle PERMITS a NUL inside a quoted string, so it loads and then
        breaks whatever reads the graph back. Nothing should reach this after
        labels_of, which is why it fails rather than cleaning up quietly."""
        self.assertEqual(common.literal("safe"), '"safe"')
        with self.assertRaises(common.IngestError) as caught:
            common.literal("verorab\x00ae")
        self.assertIn("control character", str(caught.exception))

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
