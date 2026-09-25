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
    IngestError,
    TurtleWriter,
    check_vocabulary,
    CLINICAL_STAGES,
    chembl_curie,
    drug_type_class,
    expand,
    has_control_characters,
    iri,
    literal,
    log,
    read_dataset,
    typed_literal,
)

#: Fail if more than this fraction of labels are dropped for control characters.
#: Measured at 26.06: 3 of 101,201, or 0.003%. A breach at 0.1% would be roughly
#: a hundred labels, which is an encoding change rather than stray mojibake.
MAX_REJECTED_LABEL_FRACTION = 0.001

COLUMNS = ["id", "name", "drugType", "inchiKey", "canonicalSmiles",
           "synonyms", "tradeNames", "parentId", "maximumClinicalStage"]


def labels_of(row: dict) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Every label for one molecule, as ``(usable, rejected)``.

    Each entry is ``(label, kind, source)``. `synonyms` and `tradeNames` are
    lists of ``{label, source}``; the preferred `name` has no source of its own
    and is attributed to the release.

    A label carrying a control character is rejected rather than cleaned. Three
    exist at 26.06, all upstream mojibake of a registered-trademark sign, and
    stripping the byte is not the harmless repair it looks like:
    ``verorab\x00ae`` becomes ``verorabae``, fusing two fragments into a word no
    source ever wrote. That is the "confident wrong answer" this ingest refuses
    everywhere else -- it would match nothing, and if it ever did match it would
    match falsely. Nor is the corruption consistent enough to repair: one label
    has ``\x00ae``, one a bare ``\x00``, one two NULs in a row, so recovering the
    sign would mean guessing differently each time.

    So they are dropped and RETURNED, not discarded, and the caller writes them
    out. The molecule keeps its preferred name and every other synonym.
    """
    usable: list[tuple[str, str, str]] = []
    rejected: list[tuple[str, str, str]] = []
    name = (row.get("name") or "").strip()
    if name:
        (rejected if has_control_characters(name) else usable).append(
            (name, "preferred_name", "open-targets"))
    for kind, column in (("synonym", "synonyms"), ("trade_name", "tradeNames")):
        for item in row.get(column) or []:
            label = (item.get("label") or "").strip()
            if not label:
                continue
            entry = (label, kind, (item.get("source") or "").strip())
            (rejected if has_control_characters(label) else usable).append(entry)
    return usable, rejected


def transform(input_dir: Path, out_path: Path,
              reports_dir: Path | None = None) -> dict:
    data = read_dataset(input_dir, "drug_molecule", COLUMNS)
    stats = {"molecules": 0, "with_structure": 0, "labels": 0, "distinct_labels": 0,
             "with_parent": 0, "ambiguous_labels": 0, "rejected_labels": 0,
             "triples": 0}
    seen_labels: dict[str, set[str]] = {}
    rejected_rows: list[tuple[str, str, str, str]] = []

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
                pairs.append(("sagebrain:drug_type", literal(drug_type)))

                inchi = (row.get("inchiKey") or "").strip()
                smiles = (row.get("canonicalSmiles") or "").strip()
                if inchi:
                    pairs.append(("sagebrain:inchi_key", literal(inchi)))
                if smiles:
                    pairs.append(("sagebrain:canonical_smiles", literal(smiles)))
                if inchi and smiles:
                    stats["with_structure"] += 1

                stage = (row.get("maximumClinicalStage") or "").strip()
                if stage:
                    check_vocabulary(stage, CLINICAL_STAGES,
                                     "maximumClinicalStage", "CLINICAL_STAGES")
                    # Deliberately NOT called maxClinicalStage: that name belongs to
                    # the per-indication value, and this one is a maximum over all of
                    # a molecule's indications. Conflating them is exactly the
                    # flattening the indication caveat warns about.
                    pairs.append(("sagebrain:overall_clinical_stage", literal(stage)))

                parent = (row.get("parentId") or "").strip()
                if parent and parent != chembl_id:
                    # Salt/parent relations matter for name resolution: a salt form
                    # and its parent share most labels, so a consumer that resolved
                    # to the salt can climb to the parent rather than treating the
                    # two as unrelated compounds.
                    pairs.append(("sagebrain:parent_molecule",
                                  iri(expand(chembl_curie(parent)))))
                    stats["with_parent"] += 1

                # A label can repeat within one molecule (the same string as both
                # a synonym and a trade name, or twice from different sources), so
                # alt labels are deduplicated per molecule. RDF would treat the
                # repeats as one triple anyway; emitting them just inflates the file
                # and the triple count that acceptance checks compare against.
                emitted_here: set[str] = set()
                usable, rejected = labels_of(row)
                for label, kind, source in rejected:
                    stats["rejected_labels"] += 1
                    rejected_rows.append((chembl_id, kind, source, repr(label)))
                for label, _kind, _source in usable:
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

    total = stats["labels"] + stats["rejected_labels"]
    if total and stats["rejected_labels"] / total > MAX_REJECTED_LABEL_FRACTION:
        raise IngestError(
            f"{stats['rejected_labels'] / total:.2%} of labels carry control "
            f"characters, above the {MAX_REJECTED_LABEL_FRACTION:.1%} threshold. "
            "At that rate this is an encoding change in the release rather than a "
            "few mojibake trade marks, and dropping that many labels would quietly "
            "gut the name index -- check the source before raising the bar."
        )

    if reports_dir is not None:
        reports_dir.mkdir(parents=True, exist_ok=True)
        report = reports_dir / "rejected_labels.tsv"
        report.write_text(
            "chembl_id\tkind\tsource\tlabel_repr\n"
            + "".join("\t".join(row) + "\n" for row in sorted(rejected_rows)),
            encoding="utf-8")
        if stats["rejected_labels"]:
            log(f"  {stats['rejected_labels']} label(s) dropped for control "
                f"characters -> {report}")

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
    transform(indir, workdir / "rdf" / "molecules.ttl", workdir / "reports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
