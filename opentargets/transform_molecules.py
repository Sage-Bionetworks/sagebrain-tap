"""Emit `molecules.ttl`: ChEMBL molecules with their names, structures and modality.

This is the node layer the other two transforms hang off, and the reason the whole
ingest is worth doing: a compound named in free text anywhere becomes an identifier.

## Names are the payload, not decoration

22,407 molecules carry 101,198 (label, molecule) pairs across `name`, `synonyms`
and `tradeNames` -- 97,838 of them distinct once case is folded. That index is what
lets `SELUMETINIB` and `AZD-6244` resolve to the same CHEMBL1614701, which no
amount of string matching between two local datasets can do. Every label is
emitted as `skos:altLabel` so the resolution is queryable in SPARQL, and
`export_label_index.py` writes the same index as a TSV for consumers that would
rather join a table.

## Ambiguity is kept, not resolved

A lowercased label can name more than one molecule (`CHEMBL1200692` and
`CHEMBL1201247` share one). This transform does not pick a winner: it emits every
molecule's own labels, and the exported index records the ambiguity so a consumer
decides. Silently choosing would turn an unresolvable string into a confident
wrong answer.

Usage:
    python -m opentargets.transform_molecules --release 26.06
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .common import (
    DEFAULT_RELEASE,
    TurtleWriter,
    check_vocabulary,
    CLINICAL_STAGES,
    chembl_curie,
    drug_type_class,
    expand,
    iri,
    literal,
    log,
    read_dataset,
    typed_literal,
)

COLUMNS = ["id", "name", "drugType", "inchiKey", "canonicalSmiles",
           "synonyms", "tradeNames", "parentId", "maximumClinicalStage"]


def labels_of(row: dict) -> list[tuple[str, str, str]]:
    """Every label for one molecule as ``(label, kind, source)``.

    `synonyms` and `tradeNames` are lists of ``{label, source}``; the preferred
    `name` has no source of its own and is attributed to the release.
    """
    out: list[tuple[str, str, str]] = []
    name = (row.get("name") or "").strip()
    if name:
        out.append((name, "preferred_name", "open-targets"))
    for kind, column in (("synonym", "synonyms"), ("trade_name", "tradeNames")):
        for item in row.get(column) or []:
            label = (item.get("label") or "").strip()
            if label:
                out.append((label, kind, (item.get("source") or "").strip()))
    return out


def transform(input_dir: Path, out_path: Path) -> dict:
    data = read_dataset(input_dir, "drug_molecule", COLUMNS)
    stats = {"molecules": 0, "with_structure": 0, "labels": 0, "distinct_labels": 0,
             "with_parent": 0, "ambiguous_labels": 0, "triples": 0}
    seen_labels: dict[str, set[str]] = {}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        writer = TurtleWriter(handle)
        writer.comment(
            "Open Targets drug_molecule -> ChEMBL molecule nodes.\n"
            "Labels: rdfs:label is the preferred name; every synonym and trade name\n"
            "is a skos:altLabel. drugType is kept verbatim -- see DRUG_TYPE_CLASS in\n"
            "common.py for why only 'Small molecule' gets a narrower Biolink class."
        )
        for batch in data.to_batches(columns=COLUMNS, batch_size=20_000):
            for row in batch.to_pylist():
                chembl_id = (row.get("id") or "").strip()
                if not chembl_id:
                    continue
                subject = iri(expand(chembl_curie(chembl_id)))
                drug_type = (row.get("drugType") or "").strip()
                pairs = [("a", drug_type_class(drug_type))]

                name = (row.get("name") or "").strip()
                if name:
                    pairs.append(("rdfs:label", literal(name)))
                pairs.append(("sagebrain:drugType", literal(drug_type)))

                inchi = (row.get("inchiKey") or "").strip()
                smiles = (row.get("canonicalSmiles") or "").strip()
                if inchi:
                    pairs.append(("sagebrain:inchiKey", literal(inchi)))
                if smiles:
                    pairs.append(("sagebrain:canonicalSmiles", literal(smiles)))
                if inchi and smiles:
                    stats["with_structure"] += 1

                stage = (row.get("maximumClinicalStage") or "").strip()
                if stage:
                    check_vocabulary(stage, frozenset(CLINICAL_STAGES),
                                     "maximumClinicalStage", "CLINICAL_STAGES")
                    # Deliberately NOT called maxClinicalStage: that name belongs to
                    # the per-indication value, and this one is a maximum over all of
                    # a molecule's indications. Conflating them is exactly the
                    # flattening the indication caveat warns about.
                    pairs.append(("sagebrain:maximumClinicalStage", literal(stage)))

                parent = (row.get("parentId") or "").strip()
                if parent and parent != chembl_id:
                    # Salt/parent relations matter for name resolution: a salt form
                    # and its parent share most labels, so a consumer that resolved
                    # to the salt can climb to the parent rather than treating the
                    # two as unrelated compounds.
                    pairs.append(("sagebrain:parentMolecule",
                                  iri(expand(chembl_curie(parent)))))
                    stats["with_parent"] += 1

                # A label can repeat within one molecule (the same string as both
                # a synonym and a trade name, or twice from different sources), so
                # alt labels are deduplicated per molecule. RDF would treat the
                # repeats as one triple anyway; emitting them just inflates the file
                # and the triple count that acceptance checks compare against.
                emitted_here: set[str] = set()
                for label, _kind, _source in labels_of(row):
                    seen_labels.setdefault(label.casefold(), set()).add(chembl_id)
                    stats["labels"] += 1
                    if label != name and label not in emitted_here:
                        emitted_here.add(label)
                        pairs.append(("skos:altLabel", literal(label)))

                writer.statements(subject, pairs)
                stats["molecules"] += 1

        stats["triples"] = writer.triples

    stats["distinct_labels"] = len(seen_labels)
    stats["ambiguous_labels"] = sum(1 for ids in seen_labels.values() if len(ids) > 1)
    log(f"  {stats['molecules']:,} molecules, {stats['with_structure']:,} with "
        f"InChIKey+SMILES, {stats['with_parent']:,} with a parent")
    log(f"  {stats['labels']:,} labels ({stats['distinct_labels']:,} distinct folded, "
        f"{stats['ambiguous_labels']:,} ambiguous)")
    log(f"  {stats['triples']:,} triples -> {out_path}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", default=DEFAULT_RELEASE)
    parser.add_argument("--indir", type=Path, default=None)
    parser.add_argument("--workdir", type=Path, default=None,
                        help="Output root (default: opentargets/<release>/data)")
    args = parser.parse_args()

    indir = args.indir or Path("opentargets/input") / args.release
    workdir = args.workdir or Path("opentargets") / args.release / "data"
    log(f"Transforming drug_molecule from {indir}")
    transform(indir, workdir / "rdf" / "molecules.ttl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
