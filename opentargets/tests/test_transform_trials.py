"""The trial transform's decisions, on a synthetic release small enough to read.

Every case here is one where a wrong answer would be silent: a record kind that
should not have become a node, a manufactured day asserted as fact, a placeholder
date kept, a disease node emitted twice, or a vocabulary value dropped instead of
raised. The acceptance checks verify the real release against a loaded graph;
these verify the rules that produce it.
"""

import datetime
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from opentargets import transform_trials
from opentargets.common import IngestError


def _report(report_id, **overrides):
    row = {
        "id": report_id,
        "type": "CLINICAL_TRIAL",
        "source": "AACT",
        "clinicalStage": "PHASE_2",
        "drugs": [{"drugFromSource": "selumetinib", "drugId": "CHEMBL1614701"}],
        "diseases": [{"diseaseFromSource": "plexiform neurofibroma",
                      "diseaseId": "EFO_0000658"}],
        "trialOfficialTitle": f"A study called {report_id}",
        "trialOverallStatus": "COMPLETED",
        "trialWhyStopped": None,
        "trialStopReasonCategories": [],
        "qualityControls": [],
        "hasExpertReview": False,
        "trialStartDate": datetime.date(2016, 1, 31),
        "url": f"https://clinicaltrials.gov/study/{report_id.upper()}",
    }
    row.update(overrides)
    return row


class TrialTransformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.indir = self.root / "input"
        self.out = self.root / "rdf" / "trials.ttl"
        self.reports = self.root / "reports"

    def _write(self, name, rows, schema):
        directory = self.indir / name
        directory.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows, schema=schema),
                       directory / f"{name}.parquet")

    def build(self, reports, indications=(("EFO_0000658",),), diseases=None):
        report_schema = pa.schema([
            ("id", pa.large_string()), ("type", pa.large_string()),
            ("source", pa.large_string()), ("clinicalStage", pa.large_string()),
            ("drugs", pa.large_list(pa.struct([
                ("drugFromSource", pa.large_string()), ("drugId", pa.large_string())]))),
            ("diseases", pa.large_list(pa.struct([
                ("diseaseFromSource", pa.large_string()),
                ("diseaseId", pa.large_string())]))),
            ("trialOfficialTitle", pa.large_string()),
            ("trialOverallStatus", pa.large_string()),
            ("trialWhyStopped", pa.large_string()),
            ("trialStopReasonCategories", pa.large_list(pa.large_string())),
            ("qualityControls", pa.large_list(pa.large_string())),
            ("hasExpertReview", pa.bool_()),
            ("trialStartDate", pa.date32()), ("url", pa.large_string()),
        ])
        self._write("clinical_report", list(reports), report_schema)

        indication_schema = pa.schema([
            ("id", pa.large_string()), ("drugId", pa.large_string()),
            ("diseaseId", pa.large_string()),
            ("maxClinicalStage", pa.large_string()),
            ("clinicalReportIds", pa.large_list(pa.large_string()))])
        self._write("clinical_indication", [
            {"id": f"i{index}", "drugId": "CHEMBL1614701", "diseaseId": disease_id,
             "maxClinicalStage": "PHASE_2", "clinicalReportIds": []}
            for index, (disease_id,) in enumerate(indications)], indication_schema)

        disease_schema = pa.schema([
            ("id", pa.large_string()), ("name", pa.large_string()),
            ("dbXRefs", pa.large_list(pa.large_string())),
            ("ancestors", pa.large_list(pa.large_string())),
            ("therapeuticAreas", pa.large_list(pa.large_string())),
            ("exactSynonyms", pa.large_list(pa.large_string()))])
        self._write("disease", diseases if diseases is not None else [
            {"id": "EFO_0000658", "name": "plexiform neurofibroma", "dbXRefs": [],
             "ancestors": [], "therapeuticAreas": [], "exactSynonyms": []},
            {"id": "MONDO_0017827", "name": "MPNST", "dbXRefs": [], "ancestors": [],
             "therapeuticAreas": [], "exactSynonyms": ["nerve sheath tumour"]},
        ], disease_schema)

    def run_transform(self, reports, **kwargs):
        self.build(reports, **kwargs)
        stats = transform_trials.transform(self.indir, self.out, self.reports,
                                           release="26.06")
        return stats, self.out.read_text()

    # ── scope ────────────────────────────────────────────────────────────────

    def test_only_clinical_trials_with_a_chembl_drug_become_nodes(self):
        """The two filters are about identity: the other record kinds have no id
        that survives a release, and a drug with no ChEMBL id has no node to
        point at."""
        stats, turtle = self.run_transform([
            _report("nct00000001"),
            _report("019909s020lbl.pdf", type="DRUG_LABEL"),
            _report("nct00000002", drugs=[
                {"drugFromSource": "mesenchymal stem cells", "drugId": None}]),
        ])
        self.assertEqual(stats["rows"], 3)
        self.assertEqual(stats["trial_rows"], 2)
        self.assertEqual(stats["trials"], 1)
        self.assertEqual(stats["skipped_no_chembl_drug"], 1)
        self.assertIn("clinicaltrials:NCT00000001", turtle)
        self.assertNotIn("NCT00000002", turtle)
        self.assertNotIn("019909s020lbl", turtle)

    def test_a_new_record_kind_fails_rather_than_narrowing_scope_quietly(self):
        """A fifth `type` would change what the layer contains without changing
        any code, which is the one failure that looks like sparse data."""
        self.build([_report("nct00000001", type="EXPANDED_ACCESS")])
        with self.assertRaises(IngestError) as caught:
            transform_trials.transform(self.indir, self.out, self.reports)
        self.assertIn("REPORT_TYPES", str(caught.exception))

    def test_a_trial_mapping_no_disease_is_still_a_node(self):
        stats, turtle = self.run_transform([_report("nct00000001", diseases=[])])
        self.assertEqual(stats["trials"], 1)
        self.assertEqual(stats["drug_only_trials"], 1)
        self.assertEqual(stats["disease_links"], 0)
        self.assertIn("sagebrain:trial_drug", turtle)
        self.assertNotIn("clinical_trial_conditions", turtle)

    def test_unmapped_mentions_are_counted_not_guessed(self):
        stats, _ = self.run_transform([_report(
            "nct00000001",
            drugs=[{"drugFromSource": "selumetinib", "drugId": "CHEMBL1614701"},
                   {"drugFromSource": "antibiotics", "drugId": None}],
            diseases=[{"diseaseFromSource": "plexiform neurofibroma",
                       "diseaseId": "EFO_0000658"},
                      {"diseaseFromSource": "unspecified tumour", "diseaseId": None}])])
        self.assertEqual(stats["drug_links"], 1)
        self.assertEqual(stats["disease_links"], 1)
        self.assertEqual(stats["unmapped_drug_mentions"], 1)
        self.assertEqual(stats["unmapped_disease_mentions"], 1)

    # ── identity and labels ──────────────────────────────────────────────────

    def test_the_node_is_keyed_on_the_uppercased_registry_accession(self):
        _, turtle = self.run_transform([_report("nct02407405")])
        self.assertIn("<https://identifiers.org/clinicaltrials:NCT02407405>", turtle)

    def test_a_trial_with_no_registry_title_goes_out_unlabelled(self):
        """The source's `title` column fills the gap with generated text. An
        unlabelled node beats a fabricated label."""
        stats, turtle = self.run_transform([
            _report("nct00000001", trialOfficialTitle=None)])
        self.assertEqual(stats["unlabelled_trials"], 1)
        self.assertNotIn("rdfs:label", turtle.split("# Disease")[0])

    # ── start dates ──────────────────────────────────────────────────────────

    def test_the_manufactured_day_is_dropped_and_the_month_is_typed(self):
        """2016-01-31 is a YYYY-MM source value with a day supplied by
        normalisation. gYearMonth asserts only what the release carries."""
        _, turtle = self.run_transform([_report("nct00000001")])
        self.assertIn('sagebrain:trial_start_date "2016-01"^^xsd:gYearMonth', turtle)
        self.assertNotIn("2016-01-31", turtle)

    def test_a_placeholder_date_is_dropped_but_the_trial_is_kept(self):
        # Padded to keep the two placeholders under the 1% threshold, which is
        # the point of the threshold: a handful is upstream noise.
        stats, turtle = self.run_transform([
            _report("nct00000001", trialStartDate=datetime.date(2099, 1, 1)),
            _report("nct00000002", trialStartDate=datetime.date(1931, 6, 30)),
            _report("nct00000003", trialStartDate=datetime.date(2030, 5, 1)),
        ] + [_report(f"nct{index:08d}") for index in range(10, 310)])
        self.assertEqual(stats["trials"], 303)
        self.assertEqual(stats["implausible_dates"], 2)
        self.assertEqual(stats["dated_trials"], 301)
        self.assertIn("NCT00000001", turtle)
        self.assertNotIn("2099", turtle)
        # A planned start ten years out is real and must survive.
        self.assertIn('"2030-05"^^xsd:gYearMonth', turtle)
        listed = (self.reports / "implausible_trial_dates.tsv").read_text()
        self.assertIn("NCT00000001\t2099-01-01\t1950-2036", listed)
        self.assertIn("NCT00000002\t1931-06-30\t1950-2036", listed)

    def test_wholesale_date_breakage_fails_instead_of_dropping_quietly(self):
        """Four placeholders is upstream noise; a majority is a changed scheme,
        and dropping that many dates would read as a source that stopped
        recording them."""
        with self.assertRaises(IngestError) as caught:
            self.run_transform([
                _report(f"nct0000000{index}",
                        trialStartDate=datetime.date(2099, 1, 1))
                for index in range(1, 5)])
        self.assertIn("trialStartDate", str(caught.exception))
        # The list is the diagnostic, so it exists even though the run failed.
        self.assertEqual(
            (self.reports / "implausible_trial_dates.tsv").read_text().count("2099"), 4)

    def test_a_trial_with_no_start_date_is_neither_dated_nor_implausible(self):
        stats, _ = self.run_transform([_report("nct00000001", trialStartDate=None)])
        self.assertEqual(stats["trials"], 1)
        self.assertEqual(stats["dated_trials"], 0)
        self.assertEqual(stats["implausible_dates"], 0)

    # ── stop reasons and quality controls ────────────────────────────────────

    def test_stop_reason_text_and_every_category_reach_the_node(self):
        stats, turtle = self.run_transform([_report(
            "nct00000001", trialOverallStatus="TERMINATED",
            trialWhyStopped="Closed early for futility.",
            trialStopReasonCategories=["Negative", "Safety_Sideeffects", "Negative"],
            qualityControls=["UNVALIDATED_INDICATION", "NO_DISEASE"])])
        self.assertEqual(stats["stopped_trials"], 1)
        self.assertEqual(stats["stopped_on_unexpected_status"], 0)
        # Duplicated within one row by the source; one triple, not two.
        self.assertEqual(stats["stop_reason_categories"], 2)
        self.assertEqual(stats["quality_controls"], 2)
        self.assertIn('sagebrain:trial_stop_reason "Closed early for futility."', turtle)
        self.assertIn('sagebrain:trial_stop_reason_category "Safety_Sideeffects"', turtle)
        # Both flags, not only the one that gates clinical_indication upstream.
        self.assertIn('sagebrain:trial_quality_control "NO_DISEASE"', turtle)

    def test_unknown_vocabulary_values_name_the_constant_to_extend(self):
        for column, value, constant in (
            ("trialStopReasonCategories", "Funding_Withdrawn",
             "TRIAL_STOP_REASON_CATEGORIES"),
            ("qualityControls", "NO_INTERVENTION", "REPORT_QUALITY_CONTROLS"),
        ):
            with self.subTest(column=column):
                self.build([_report("nct00000001", **{column: [value]},
                                    trialOverallStatus="TERMINATED",
                                    trialWhyStopped="stopped")])
                with self.assertRaises(IngestError) as caught:
                    transform_trials.transform(self.indir, self.out, self.reports)
                self.assertIn(constant, str(caught.exception))

    def test_status_uses_the_biolink_slot_and_is_enforced(self):
        """The one clinical-trial slot in Biolink whose domain and range both fit:
        ClinicalTrialStatusEnum matches Open Targets' 13 values exactly."""
        _, turtle = self.run_transform([
            _report("nct00000001", trialOverallStatus="NOT_YET_RECRUITING")])
        self.assertIn('biolink:clinical_trial_overall_status "NOT_YET_RECRUITING"',
                      turtle)
        self.build([_report("nct00000001", trialOverallStatus="ENROLLING")])
        with self.assertRaises(IngestError) as caught:
            transform_trials.transform(self.indir, self.out, self.reports)
        self.assertIn("TRIAL_OVERALL_STATUSES", str(caught.exception))

    def test_a_stop_reason_on_a_running_trial_is_counted(self):
        """Status and stop reason are separate columns; every stop reason in the
        release sits on TERMINATED, WITHDRAWN or SUSPENDED. If that came apart the
        free text would be describing something other than a stop, so the count is
        surfaced rather than the pairing assumed."""
        stats, _ = self.run_transform([
            _report("nct00000001", trialOverallStatus="SUSPENDED",
                    trialWhyStopped="Paused for a safety review.",
                    trialStopReasonCategories=["Safety_Sideeffects"]),
            _report("nct00000002", trialOverallStatus="COMPLETED",
                    trialWhyStopped="Closed early for futility.",
                    trialStopReasonCategories=["Negative"]),
        ])
        self.assertEqual(stats["stopped_trials"], 2)
        self.assertEqual(stats["stopped_on_unexpected_status"], 1)

    # ── the disease pass, split with indications.ttl ─────────────────────────

    def test_only_terms_no_indication_references_get_a_node_here(self):
        """Each term is asserted exactly once across the release: indications.ttl
        owns the terms an indication references, this owns the remainder."""
        stats, turtle = self.run_transform([_report("nct00000001", diseases=[
            {"diseaseFromSource": "plexiform neurofibroma",
             "diseaseId": "EFO_0000658"},
            {"diseaseFromSource": "MPNST", "diseaseId": "MONDO_0017827"}])])
        self.assertEqual(stats["diseases"], 2)
        self.assertEqual(stats["new_disease_nodes"], 1)
        nodes = turtle.split("# Disease and phenotype nodes")[1]
        self.assertIn("obo/MONDO_0017827>\n    a biolink:Disease", nodes)
        self.assertIn('skos:altLabel "nerve sheath tumour"', nodes)
        self.assertNotIn("EFO_0000658", nodes)

    def test_a_term_the_release_does_not_describe_is_typed_but_unlabelled(self):
        stats, turtle = self.run_transform(
            [_report("nct00000001", diseases=[
                {"diseaseFromSource": "something", "diseaseId": "HP_0001234"}])],
            diseases=[])
        self.assertEqual(stats["new_disease_nodes"], 1)
        self.assertEqual(stats["unlabelled_diseases"], 1)
        self.assertIn("obo/HP_0001234>\n    a biolink:PhenotypicFeature .", turtle)


if __name__ == "__main__":
    unittest.main()
