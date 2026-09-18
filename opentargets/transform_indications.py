"""Emit `indications.ttl`: compound -> disease indications, with clinical stage.

This is the axis no other source supplies, and the one that answers the question
portal users actually have: not "is this gene a target of that compound" but "is
this compound in development, or approved, for the disease the model was derived
from".

## The predicate is deliberately weak

`biolink:treats_or_applied_or_studied_to_treat`, not `biolink:treats`. 75,293 of
the 86,468 indication rows are at a stage below APPROVAL -- PHASE_2 alone is
28,370 -- and a phase-1 trial is a compound being *studied* for a disease, not one
that treats it. Biolink has a predicate for exactly this distinction and using the
stronger one would turn every abandoned trial in the release into a therapeutic
claim.

## Stage is on the edge, never on the compound

`maxClinicalStage` is a maximum over the reports behind one drug-disease pair, so
it is a property of that pair. A drug that failed phase 3 for one disease and was
approved for another has two different stages, and hoisting either onto the drug
would erase the difference. `drug_molecule.maximumClinicalStage` -- the maximum
over *all* of a drug's indications -- is emitted by `transform_molecules.py` under
a deliberately different name for the same reason.

The report count rides along as `sagebrain:clinicalReportCount`: a stage backed by
14 reports and one backed by a single record are not equally load-bearing, and the
count is the cheapest way to say so without ingesting `clinical_report` itself.

## Disease nodes are typed by prefix

`clinical_indication` mixes ontologies: 8,591 rows point at HP phenotype terms and
430 at MP mouse-phenotype terms. Typing those `biolink:Disease` would be wrong, so
`DISEASE_NODE_CLASS` decides from the id prefix. Nodes are emitted only for terms
an indication actually references -- the release describes 47,080 terms, and the
ones nothing points at are another source's job.

Usage:
    python -m opentargets.transform_indications --release 26.06
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .common import (
    CLINICAL_STAGES,
    DEFAULT_RELEASE,
    OPENTARGETS_SOURCE,
    TurtleWriter,
    check_vocabulary,
    chembl_curie,
    disease_curie,
    disease_node_class,
    expand,
    iri,
    literal,
    log,
    read_dataset,
    typed_literal,
)

INDICATION_COLUMNS = ["drugId", "diseaseId", "maxClinicalStage", "clinicalReportIds"]
DISEASE_COLUMNS = ["id", "name", "exactSynonyms", "therapeuticAreas"]

XSD_INTEGER = "xsd:integer"


def load_disease_labels(input_dir: Path) -> dict[str, dict]:
    """Disease id -> label and exact synonyms, for the whole release.

    Read in full because the referenced set is not known until the indications
    have been scanned, and 47,080 terms of labels is small. Synonyms are kept:
    "MPNST" and "malignant peripheral nerve sheath tumor" are the same term, and a
    consumer matching free-text disease names needs both.
    """
    data = read_dataset(input_dir, "disease", DISEASE_COLUMNS)
    labels: dict[str, dict] = {}
    for batch in data.to_batches(columns=DISEASE_COLUMNS, batch_size=20_000):
        for row in batch.to_pylist():
            disease_id = (row.get("id") or "").strip()
            if disease_id:
                labels[disease_id] = {
                    "name": (row.get("name") or "").strip(),
                    "synonyms": [s.strip() for s in (row.get("exactSynonyms") or []) if s],
                    "areas": [a.strip() for a in (row.get("therapeuticAreas") or []) if a],
                }
    return labels


def transform(input_dir: Path, out_path: Path) -> dict:
    disease_labels = load_disease_labels(input_dir)
    data = read_dataset(input_dir, "clinical_indication", INDICATION_COLUMNS)
    stats = {"rows": 0, "edges": 0, "duplicate_edges": 0, "drugs": 0, "diseases": 0,
             "approval_edges": 0, "unlabelled_diseases": 0, "triples": 0}
    edges: set[tuple] = set()
    referenced: set[str] = set()
    drugs: set[str] = set()
    by_stage: dict[str, int] = {}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        writer = TurtleWriter(handle)
        writer.comment(
            "Open Targets clinical_indication -> compound/disease edges.\n"
            "Predicate is treats_or_applied_or_studied_to_treat, NOT treats: most rows\n"
            "are trials, not approvals. sagebrain:maxClinicalStage belongs to the edge,\n"
            "never to the compound -- quote a stage with its indication or not at all."
        )

        for batch in data.to_batches(columns=INDICATION_COLUMNS, batch_size=20_000):
            for row in batch.to_pylist():
                stats["rows"] += 1
                drug_id = (row.get("drugId") or "").strip()
                disease_id = (row.get("diseaseId") or "").strip()
                if not drug_id or not disease_id:
                    continue
                stage = (row.get("maxClinicalStage") or "").strip()
                check_vocabulary(stage, frozenset(CLINICAL_STAGES),
                                 "maxClinicalStage", "CLINICAL_STAGES")
                reports = len(row.get("clinicalReportIds") or [])

                key = (drug_id, disease_id, stage)
                if key in edges:
                    stats["duplicate_edges"] += 1
                    continue
                edges.add(key)
                referenced.add(disease_id)
                drugs.add(drug_id)
                by_stage[stage] = by_stage.get(stage, 0) + 1
                if stage == "APPROVAL":
                    stats["approval_edges"] += 1

                writer.blank_node([
                    ("a", "biolink:ChemicalToDiseaseOrPhenotypicFeatureAssociation"),
                    ("biolink:subject", iri(expand(chembl_curie(drug_id)))),
                    ("biolink:object", iri(expand(disease_curie(disease_id)))),
                    ("biolink:predicate",
                     "biolink:treats_or_applied_or_studied_to_treat"),
                    ("sagebrain:maxClinicalStage", literal(stage)),
                    ("sagebrain:clinicalReportCount",
                     typed_literal(str(reports), XSD_INTEGER)),
                    ("biolink:primary_knowledge_source", OPENTARGETS_SOURCE),
                ])
                stats["edges"] += 1

        writer.comment("Disease and phenotype nodes for every term referenced above.")
        for disease_id in sorted(referenced):
            info = disease_labels.get(disease_id)
            pairs = [("a", disease_node_class(disease_id))]
            if info and info["name"]:
                pairs.append(("rdfs:label", literal(info["name"])))
            else:
                # An indication pointing at a term the release does not describe is
                # a dangling reference upstream. Emitted anyway, typed but unlabelled,
                # so it is visible rather than dropped -- and counted below.
                stats["unlabelled_diseases"] += 1
            for synonym in sorted(set(info["synonyms"])) if info else []:
                pairs.append(("skos:altLabel", literal(synonym)))
            writer.statements(iri(expand(disease_curie(disease_id))), pairs)

        stats["diseases"] = len(referenced)
        stats["drugs"] = len(drugs)
        stats["triples"] = writer.triples

    top = sorted(by_stage.items(), key=lambda kv: -kv[1])[:4]
    log(f"  {stats['rows']:,} indication rows -> {stats['edges']:,} edges "
        f"({stats['duplicate_edges']:,} duplicates collapsed)")
    log(f"  {stats['drugs']:,} drugs x {stats['diseases']:,} diseases; "
        f"{stats['approval_edges']:,} at APPROVAL")
    log(f"  top stages: {', '.join(f'{s}={c:,}' for s, c in top)}")
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
    log(f"Transforming clinical_indication from {indir}")
    transform(indir, workdir / "rdf" / "indications.ttl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
