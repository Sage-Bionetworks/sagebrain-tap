"""Emit `trials.ttl`: the clinical trials behind the indication edges, as nodes.

`clinical_indication` flattens every trial for a drug-disease pair into one
`sagebrain:max_clinical_stage` and a count. This layer restores what that
maximum cannot carry: which trials, when they started, and why 21,876 of them
stopped. See [TRIALS.md](TRIALS.md) for the scope decisions; this module
implements them.

## Two filters, both about identity

**Only `type = CLINICAL_TRIAL`** -- 230,990 of 289,955 records. The other three
kinds are provenance about a drug rather than trial evidence, and none of them
has an identifier that survives a release: trial ids are uniformly `nct<digits>`
with no exceptions, while curated resources, drug labels and regulatory records
mix DailyMed UUIDs, sha256 hashes and raw strings such as `019909s020lbl.pdf` or
a bare URL. Only trials get a CURIE worth minting.

**Only trials that join an emitted molecule** -- 193,469 of those (83.8%). Their
9,578 distinct drugs are all already in `drug_molecule`, so no trial node dangles.
The 37,521 dropped trials do all name a drug; what they lack is a ChEMBL id for
it, and they concentrate in cell, tissue, microbiota and blood-product therapies
(mesenchymal stem cells, CAR-T, platelet-rich plasma, faecal microbiota
transplant, convalescent plasma) plus class-level names like `antibiotics`. The
filter is an identity limit inherited from ChEMBL, not a judgement about
intervention type.

## Start dates say only what the release carries

`trialStartDate` is a `date32`, but its day is manufactured for older records:
92.6% of pre-2000 and 91.7% of 2000-2009 start dates fall on the last day of a
month, against 10.5% for 2020 and later, because the source value is `YYYY-MM`
and normalisation supplies the day. So the value is emitted as `xsd:gYearMonth`.
Truncating is not a loss of information -- it is a refusal to assert a day the
release invented.

Range is a separate question from precision, and it is handled separately.
Future dates are mostly real (114 in-scope trials start in 2027-2030, 92 of them
`NOT_YET_RECRUITING`), so the window clears planned starts and catches only the
placeholders: at 26.06, four in-scope trials -- 1931-06-30, 2040-01-01,
2050-01-31 and 2099-01-01. They are listed in
`reports/implausible_trial_dates.tsv` and lose their date, not their node. Two
more out-of-window rows exist in the release and never reach this check, because
the drug filter removed them first.

## Status is what makes a stop reason mean something

`trialOverallStatus` is filled on every trial and empty on every other record
kind, and all 21,876 stop reasons sit on `TERMINATED`, `WITHDRAWN` or `SUSPENDED`.
A `Negative` category on a trial that halted midway is not the same claim as one
on a trial that never enrolled a participant, and only the status separates them.

It is emitted under `biolink:clinical_trial_overall_status`, a real Biolink slot:
its domain is `clinical trial` and its range `ClinicalTrialStatusEnum` is
value-for-value identical to the 13 values Open Targets uses. That makes it the
one clinical-trial slot in Biolink that fits this data with no compromise at all,
which is worth saying out loud given the three below that do not.

## Trials are nodes; the links are node properties, not associations

A trial is a study, not a claim, so it is a `biolink:ClinicalTrial` carrying
properties -- unlike mechanisms and indications, which are reified associations
because each *is* an assertion with a subject and an object.

The two link slots are deliberately asymmetric. `biolink:clinical_trial_conditions`
has exactly the right domain and range for the disease side. Biolink's matching
`clinical_trial_interventions` does not: its range is `clinical intervention`,
and these compounds are `biolink:ChemicalEntity` -- this ingest does not even type
them `biolink:Drug` (see the Compound class in schema/opentargets.yaml). So the
drug side takes `sagebrain:trial_drug`, named after the source column, rather
than a Biolink slot whose declared range the objects do not satisfy.

Two more Biolink slots were checked and rejected the same way.
`clinical_trial_phase` has range `ResearchPhaseEnum`, a different vocabulary that
cannot express `APPROVAL` or `UNKNOWN`, so the stage takes
`sagebrain:trial_clinical_stage`. `clinical_trial_start_date` has range `string`,
which cannot carry the month-precision claim, so the date takes
`sagebrain:trial_start_date` typed `xsd:gYearMonth`.

## What is read but not emitted

`source` is `AACT` on every in-scope trial, `hasExpertReview` is false on every
one of them, and `url` is `https://clinicaltrials.gov/study/<NCT>` -- derivable
from the node's own IRI. All three are read anyway and reported, so those three
claims stay checked against each release rather than asserted once in prose.

Usage:
    python -m opentargets.transform_trials --release 26.06
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .common import (
    CLINICAL_STAGES,
    DEFAULT_RELEASE,
    REPORT_QUALITY_CONTROLS,
    REPORT_TYPES,
    TRIAL_OVERALL_STATUSES,
    TRIAL_RECORD_TYPE,
    TRIAL_STOP_REASON_CATEGORIES,
    TRIAL_STOPPED_STATUSES,
    IngestError,
    TurtleWriter,
    check_vocabulary,
    chembl_curie,
    disease_curie,
    expand,
    iri,
    literal,
    load_disease_labels,
    log,
    read_dataset,
    trial_curie,
    trial_start_window,
    typed_literal,
    write_disease_nodes,
)

REPORT_COLUMNS = [
    "id", "type", "source", "clinicalStage", "drugs", "diseases",
    "trialOfficialTitle", "trialOverallStatus", "trialStartDate",
    "trialWhyStopped", "trialStopReasonCategories", "qualityControls",
    "hasExpertReview", "url",
]

#: Read only to subtract the terms `indications.ttl` already asserts.
INDICATION_COLUMNS = ["diseaseId"]

XSD_GYEARMONTH = "xsd:gYearMonth"

#: The URL every in-scope trial carries, derivable from the id. Deviations are
#: counted rather than emitted; see the module docstring.
CANONICAL_URL = "https://clinicaltrials.gov/study/{accession}"

#: Fail if more than this fraction of dated trials fall outside the plausible
#: window. Measured at 26.06: 4 of 190,705, or 0.002%. A breach at 1% would be
#: roughly 1,900 trials, which is a changed date scheme rather than a handful of
#: placeholder rows, and dropping that many dates quietly would read as a source
#: that stopped recording them.
MAX_IMPLAUSIBLE_DATE_FRACTION = 0.01


def indication_disease_ids(input_dir: Path) -> set[str]:
    """Every term `clinical_indication` references, so trials can skip them.

    Disease nodes are asserted exactly once across the release. `indications.ttl`
    owns the 3,749 terms an indication references; this module owns only the 310
    further terms that just a trial references. Reading one column of 86,468 rows
    is far cheaper than the reverse arrangement, where the indication transform
    would have to re-scan 74 MB of reports and duplicate this module's scope
    filter to know which trial references count.
    """
    data = read_dataset(input_dir, "clinical_indication", INDICATION_COLUMNS)
    column = data.to_table(columns=INDICATION_COLUMNS).column("diseaseId")
    return {value.strip() for value in column.to_pylist() if value}


def _mapped_ids(entries, key: str) -> tuple[set[str], int]:
    """Ids from a list of `{...FromSource, ...Id}` structs, plus unmapped mentions.

    Both `drugs` and `diseases` name the entity in free text and carry an
    ontology id only when the release resolved it. The unmapped mentions are
    counted, never guessed at: inside the trials this layer emits, 16,474 of
    356,755 drug mentions (4.6%) and 61,777 of 240,585 disease mentions (25.7%)
    have no id, and inventing one would be worse than the gap.

    Those are IN-SCOPE rates, which is what this function returns. Over the whole
    dataset the figures are 16.7% and 23.8%, and the two move in opposite
    directions: the drug rate falls sharply in scope (16.7% to 4.6%) because the
    trials with no ChEMBL-resolved drug at all are exactly the ones the filter
    removed, while the disease rate rises slightly (23.8% to 25.7%) because those
    same excluded trials were, if anything, better mapped on the disease side.
    Quoting the dataset-wide numbers here would overstate how dirty the emitted
    trials are by more than threefold.
    """
    mapped: set[str] = set()
    unmapped = 0
    for entry in entries or []:
        if not entry:
            continue
        value = (entry.get(key) or "").strip()
        if value:
            mapped.add(value)
        else:
            unmapped += 1
    return mapped, unmapped


def transform(input_dir: Path, out_path: Path, reports_dir: Path,
              release: str = DEFAULT_RELEASE) -> dict:
    earliest, latest = trial_start_window(release)
    disease_labels = load_disease_labels(input_dir)
    already_emitted = indication_disease_ids(input_dir)
    data = read_dataset(input_dir, "clinical_report", REPORT_COLUMNS)

    stats = {
        "rows": 0, "trial_rows": 0, "trials": 0, "skipped_no_chembl_drug": 0,
        "drug_links": 0, "disease_links": 0, "drugs": 0, "diseases": 0,
        "new_disease_nodes": 0, "unlabelled_diseases": 0, "drug_only_trials": 0,
        "unmapped_drug_mentions": 0, "unmapped_disease_mentions": 0,
        "unlabelled_trials": 0, "dated_trials": 0, "implausible_dates": 0,
        "stopped_trials": 0, "stop_reason_categories": 0, "quality_controls": 0,
        "stopped_on_unexpected_status": 0,
        "expert_reviewed": 0, "non_canonical_urls": 0, "triples": 0,
    }
    referenced: set[str] = set()
    drugs: set[str] = set()
    by_stage: dict[str, int] = {}
    by_status: dict[str, int] = {}
    sources: dict[str, int] = {}
    implausible: list[tuple[str, str]] = []

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        writer = TurtleWriter(handle)
        writer.comment(
            "Open Targets clinical_report -> clinical trial nodes.\n"
            "Only type=CLINICAL_TRIAL, and only trials naming a ChEMBL-resolved drug:\n"
            "no other record kind has an identifier that survives a release.\n"
            "sagebrain:trial_start_date is xsd:gYearMonth because the source's day is\n"
            "manufactured for older records -- 92.6% of pre-2000 starts land on the\n"
            "last day of a month. sagebrain:trial_clinical_stage is a THIRD stage slot,\n"
            "scoped to one trial: not max_clinical_stage (a drug-disease pair) and not\n"
            "overall_clinical_stage (a molecule)."
        )

        for batch in data.to_batches(columns=REPORT_COLUMNS, batch_size=20_000):
            for row in batch.to_pylist():
                stats["rows"] += 1
                record_type = (row.get("type") or "").strip()
                check_vocabulary(record_type, REPORT_TYPES, "type", "REPORT_TYPES")
                if record_type != TRIAL_RECORD_TYPE:
                    continue
                stats["trial_rows"] += 1

                drug_ids, unmapped_drugs = _mapped_ids(row.get("drugs"), "drugId")
                if not drug_ids:
                    # Every dropped trial does name a drug -- it just has no ChEMBL
                    # id for it. An edge to a molecule this graph never asserted
                    # would join to nothing and read as absent data.
                    stats["skipped_no_chembl_drug"] += 1
                    continue

                curie = trial_curie(row.get("id") or "")
                accession = curie.partition(":")[2]
                stats["trials"] += 1
                stats["unmapped_drug_mentions"] += unmapped_drugs
                source = (row.get("source") or "").strip()
                sources[source] = sources.get(source, 0) + 1
                if row.get("hasExpertReview"):
                    stats["expert_reviewed"] += 1
                if (row.get("url") or "").strip() != CANONICAL_URL.format(
                        accession=accession):
                    stats["non_canonical_urls"] += 1

                stage = (row.get("clinicalStage") or "").strip()
                check_vocabulary(stage, CLINICAL_STAGES,
                                 "clinicalStage", "CLINICAL_STAGES")
                by_stage[stage] = by_stage.get(stage, 0) + 1

                pairs = [("a", "biolink:ClinicalTrial")]
                title = (row.get("trialOfficialTitle") or "").strip()
                if title:
                    pairs.append(("rdfs:label", literal(title)))
                else:
                    # 3,282 in-scope trials carry no registry title. The `title`
                    # column fills the gap with a generated string ("Report in
                    # Phase 3 stage for 2 molecules and Retinitis Pigmentosa"),
                    # which is derived text, not a name the registry gave the
                    # trial -- so the node goes out unlabelled instead.
                    stats["unlabelled_trials"] += 1
                pairs.append(("sagebrain:trial_clinical_stage", literal(stage)))

                status = (row.get("trialOverallStatus") or "").strip()
                check_vocabulary(status, TRIAL_OVERALL_STATUSES,
                                 "trialOverallStatus", "TRIAL_OVERALL_STATUSES")
                by_status[status] = by_status.get(status, 0) + 1
                pairs.append(("biolink:clinical_trial_overall_status",
                              literal(status)))

                start = row.get("trialStartDate")
                if start is not None:
                    if earliest <= start.year <= latest:
                        stats["dated_trials"] += 1
                        pairs.append(("sagebrain:trial_start_date",
                                      typed_literal(f"{start.year:04d}-{start.month:02d}",
                                                    XSD_GYEARMONTH)))
                    else:
                        stats["implausible_dates"] += 1
                        implausible.append((accession, start.isoformat()))

                why_stopped = (row.get("trialWhyStopped") or "").strip()
                categories = sorted(
                    {c.strip() for c in (row.get("trialStopReasonCategories") or []) if c})
                if why_stopped:
                    stats["stopped_trials"] += 1
                    if status not in TRIAL_STOPPED_STATUSES:
                        # Counted rather than raised: the two columns coming apart
                        # would make the free text describe something other than a
                        # stop, and that is a finding about the release, not a
                        # reason to refuse it. Acceptance check 20 is the gate.
                        stats["stopped_on_unexpected_status"] += 1
                    pairs.append(("sagebrain:trial_stop_reason", literal(why_stopped)))
                for category in categories:
                    check_vocabulary(category, TRIAL_STOP_REASON_CATEGORIES,
                                     "trialStopReasonCategories",
                                     "TRIAL_STOP_REASON_CATEGORIES")
                    pairs.append(("sagebrain:trial_stop_reason_category",
                                  literal(category)))
                    stats["stop_reason_categories"] += 1

                for control in sorted({c.strip()
                                       for c in (row.get("qualityControls") or []) if c}):
                    check_vocabulary(control, REPORT_QUALITY_CONTROLS,
                                     "qualityControls", "REPORT_QUALITY_CONTROLS")
                    pairs.append(("sagebrain:trial_quality_control", literal(control)))
                    stats["quality_controls"] += 1

                for drug_id in sorted(drug_ids):
                    pairs.append(("sagebrain:trial_drug",
                                  iri(expand(chembl_curie(drug_id)))))
                    stats["drug_links"] += 1
                drugs |= drug_ids

                disease_ids, unmapped_diseases = _mapped_ids(
                    row.get("diseases"), "diseaseId")
                stats["unmapped_disease_mentions"] += unmapped_diseases
                if not disease_ids:
                    # 50,971 in-scope trials map no disease at all. They are still
                    # trials of a known drug, so they become drug-only nodes rather
                    # than being dropped for an absence that is the source's.
                    stats["drug_only_trials"] += 1
                for disease_id in sorted(disease_ids):
                    pairs.append(("biolink:clinical_trial_conditions",
                                  iri(expand(disease_curie(disease_id)))))
                    stats["disease_links"] += 1
                referenced |= disease_ids

                writer.statements(iri(expand(curie)), pairs)

        new_terms = referenced - already_emitted
        writer.comment(
            "Disease and phenotype nodes for the terms ONLY a trial references.\n"
            "indications.ttl asserts the rest; between them the disease pass covers\n"
            "every term referenced by either, each emitted exactly once.")
        stats["unlabelled_diseases"] = write_disease_nodes(
            writer, new_terms, disease_labels)

        stats["drugs"] = len(drugs)
        stats["diseases"] = len(referenced)
        stats["new_disease_nodes"] = len(new_terms)
        stats["triples"] = writer.triples

    # Written before the threshold is applied: if the run is about to fail, this
    # list is the diagnostic, and producing it only on success would withhold it
    # from exactly the case that needs it.
    reports_dir.mkdir(parents=True, exist_ok=True)
    report = reports_dir / "implausible_trial_dates.tsv"
    report.write_text(
        "trial\ttrial_start_date\tplausible_window\n"
        + "".join(f"{accession}\t{date}\t{earliest}-{latest}\n"
                  for accession, date in sorted(implausible)),
        encoding="utf-8")

    dated = stats["dated_trials"] + stats["implausible_dates"]
    fraction = stats["implausible_dates"] / dated if dated else 0.0
    if fraction > MAX_IMPLAUSIBLE_DATE_FRACTION:
        raise IngestError(
            f"{fraction:.2%} of trial start dates fall outside {earliest}-{latest}, "
            f"above the {MAX_IMPLAUSIBLE_DATE_FRACTION:.0%} threshold, listed in "
            f"{report}. At that rate the dates are not a handful of placeholders -- "
            f"check whether the release changed how it records trialStartDate before "
            f"widening TRIAL_START_MIN_YEAR or TRIAL_START_FUTURE_YEARS in "
            f"opentargets/common.py."
        )

    top = sorted(by_stage.items(), key=lambda kv: -kv[1])[:4]
    log(f"  {stats['rows']:,} report rows -> {stats['trial_rows']:,} trials -> "
        f"{stats['trials']:,} in scope "
        f"({stats['skipped_no_chembl_drug']:,} named no ChEMBL-resolved drug)")
    log(f"  {stats['drug_links']:,} trial-drug links over {stats['drugs']:,} drugs; "
        f"{stats['disease_links']:,} trial-disease links over {stats['diseases']:,} terms "
        f"({stats['drug_only_trials']:,} trials map no disease)")
    log(f"  {stats['new_disease_nodes']:,} disease/phenotype node(s) only a trial "
        f"references; indications.ttl asserts the rest")
    log(f"  top stages: {', '.join(f'{s}={c:,}' for s, c in top)}")
    log(f"  {stats['dated_trials']:,} start dates as gYearMonth; "
        f"{stats['implausible_dates']} outside {earliest}-{latest} dropped -> {report}")
    top_status = sorted(by_status.items(), key=lambda kv: -kv[1])[:4]
    log(f"  top statuses: {', '.join(f'{s}={c:,}' for s, c in top_status)}")
    log(f"  {stats['stopped_trials']:,} stopped, carrying "
        f"{stats['stop_reason_categories']:,} categorised reason(s) "
        f"({stats['stopped_on_unexpected_status']} on a status other than "
        f"{'/'.join(sorted(TRIAL_STOPPED_STATUSES))}); "
        f"{stats['quality_controls']:,} quality-control flag(s)")
    log(f"  {stats['unmapped_drug_mentions']:,} drug and "
        f"{stats['unmapped_disease_mentions']:,} disease mention(s) inside in-scope "
        f"trials carry no id; {stats['unlabelled_trials']:,} trial(s) have no "
        f"registry title")
    log(f"  read but not emitted: source={dict(sources)}, "
        f"{stats['expert_reviewed']} expert-reviewed, "
        f"{stats['non_canonical_urls']} non-canonical url(s)")
    if stats["unlabelled_diseases"]:
        log(f"  {stats['unlabelled_diseases']} referenced term(s) have no label in "
            f"the release's disease dataset")
    log(f"  {stats['triples']:,} triples -> {out_path}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", default=DEFAULT_RELEASE)
    parser.add_argument("--indir", type=Path, default=None)
    parser.add_argument("--workdir", type=Path, default=None)
    args = parser.parse_args()

    indir = args.indir or Path("opentargets/input") / args.release
    workdir = args.workdir or Path("opentargets") / args.release / "data"
    log(f"Transforming clinical_report trials from {indir}")
    transform(indir, workdir / "rdf" / "trials.ttl", workdir / "reports",
              release=args.release)
    return 0


if __name__ == "__main__":
    sys.exit(main())
