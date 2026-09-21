"""Emit `mechanisms.ttl`: compound -> gene mechanism-of-action edges.

## Why the gene side is HGNC, not Ensembl

Open Targets keys targets on Ensembl gene ids. Reactome, already ingested here,
keys genes on HGNC, and so does the NF-OSI graph this layer is meant to join. So
the Ensembl id is resolved on the way in -- exactly as `transform_associations.py`
resolves UniProt -- and kept on the association as `biolink:original_object`.
Measured at 26.06: 1,548 of 1,550 distinct target ids resolve (99.9%), so the
layer's gene-side join is as good as the pathway layer's.

Both endpoints are typed nodes, per the same rule Reactome follows: a consumer can
label a gene without reading the association table, and a query that joins on a
gene cannot silently match an identifier the ingest never asserted.

## `targets` is a list, and what it means depends on `targetType`

For a `single protein` row the list is one gene and the edge means what it looks
like. For `protein family`, `protein complex`, `selectivity group` and the rest,
the list enumerates the group's MEMBERS -- trametinib's "MEK1/2 inhibitor" row
lists MAP2K1 and MAP2K2 -- so the edge asserts membership of a named target group,
not a separately measured interaction with each gene. `targetType` rides on every
edge so a query wanting strict pairs can filter, and the manifest records how many
edges are of each kind.

## `chemblIds` is also a list

One mechanism row can name many drugs; 6,500 rows cover 5,820 molecules. The row
is fanned out per drug, and the release additionally contains genuinely duplicate
rows (ribociclib's CDK4 mechanism appears twice), so edges are deduplicated on the
full tuple and the count is reported rather than left to inflate the graph.

Usage:
    python -m opentargets.transform_mechanisms --release 26.06
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import hgnc

from .common import (
    ACTION_TYPES,
    DEFAULT_RELEASE,
    OPENTARGETS_SOURCE,
    SINGLE_TARGET_TYPES,
    TARGET_TYPES,
    HgncResolver,
    IngestError,
    TurtleWriter,
    check_vocabulary,
    chembl_curie,
    ensembl_curie,
    expand,
    hgnc_curie,
    iri,
    literal,
    log,
    read_dataset,
)

COLUMNS = ["actionType", "mechanismOfAction", "chemblIds",
           "targetName", "targetType", "targets"]

#: Fail if more than this fraction of target lookups do not resolve to HGNC.
#: Reactome uses the same 10% rule for UniProt. The measured rate at 26.06 is
#: 0.1%, so this trips only on a real upstream change of key scheme.
MAX_UNRESOLVED_FRACTION = 0.10


def transform(input_dir: Path, out_path: Path, resolver: HgncResolver,
              reports_dir: Path) -> dict:
    data = read_dataset(input_dir, "drug_mechanism_of_action", COLUMNS)
    stats = {"rows": 0, "edges": 0, "duplicate_edges": 0, "genes": 0,
             "single_target_edges": 0, "group_target_edges": 0,
             "rows_without_targets": 0, "triples": 0}
    edges: set[tuple] = set()
    genes: dict[str, str] = {}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        writer = TurtleWriter(handle)
        writer.comment(
            "Open Targets drug_mechanism_of_action -> compound/gene edges.\n"
            "Ensembl target ids are resolved to HGNC so genes are one kind of node\n"
            "across this repo; the Ensembl id is kept as biolink:original_object.\n"
            "sagebrain:target_type distinguishes a single-protein edge from membership\n"
            "of a named target family or complex -- they are not the same claim."
        )

        for batch in data.to_batches(columns=COLUMNS, batch_size=20_000):
            for row in batch.to_pylist():
                stats["rows"] += 1
                action = (row.get("actionType") or "").strip()
                check_vocabulary(action, ACTION_TYPES, "actionType", "ACTION_TYPES")
                target_type = (row.get("targetType") or "").strip()
                check_vocabulary(target_type, TARGET_TYPES, "targetType", "TARGET_TYPES")
                mechanism = (row.get("mechanismOfAction") or "").strip()
                target_name = (row.get("targetName") or "").strip()

                target_ids = [t for t in (row.get("targets") or []) if t]
                if not target_ids:
                    # A mechanism with no gene target is real (vaccine antigens,
                    # some cell therapies). Counted, not emitted: an edge needs
                    # two endpoints.
                    stats["rows_without_targets"] += 1
                    continue

                for chembl_id in (row.get("chemblIds") or []):
                    if not chembl_id:
                        continue
                    for ensembl_id in target_ids:
                        for hgnc_id in resolver.resolve(ensembl_id):
                            key = (chembl_id, hgnc_id, action, mechanism,
                                   target_type, ensembl_id)
                            if key in edges:
                                stats["duplicate_edges"] += 1
                                continue
                            edges.add(key)
                            genes[hgnc_id] = resolver.symbol(hgnc_id)
                            writer.blank_node([
                                ("a", "biolink:ChemicalToGeneAssociation"),
                                ("biolink:subject",
                                 iri(expand(chembl_curie(chembl_id)))),
                                ("biolink:object", iri(expand(hgnc_curie(hgnc_id)))),
                                ("biolink:predicate", "biolink:affects"),
                                ("sagebrain:action_type", literal(action)),
                                ("sagebrain:mechanism_of_action", literal(mechanism)),
                                ("sagebrain:target_type", literal(target_type)),
                                ("sagebrain:target_name", literal(target_name)),
                                ("biolink:original_object",
                                 literal(ensembl_curie(ensembl_id))),
                                ("biolink:primary_knowledge_source", OPENTARGETS_SOURCE),
                            ])
                            stats["edges"] += 1
                            if target_type in SINGLE_TARGET_TYPES:
                                stats["single_target_edges"] += 1
                            else:
                                stats["group_target_edges"] += 1

        # Gene nodes last, so the file is self-contained: every object of an
        # association above is a typed node with a symbol below.
        writer.comment("Gene nodes for every mechanism target reached above.")
        for hgnc_id, symbol in sorted(genes.items()):
            pairs = [("a", "biolink:Gene"), ("biolink:in_taxon", "NCBITaxon:9606")]
            if symbol:
                pairs.insert(1, ("rdfs:label", literal(symbol)))
            writer.statements(iri(expand(hgnc_curie(hgnc_id))), pairs)
        stats["genes"] = len(genes)
        stats["triples"] = writer.triples

    if resolver.unresolved_fraction > MAX_UNRESOLVED_FRACTION:
        raise IngestError(
            f"{resolver.unresolved_fraction:.1%} of mechanism target lookups did not "
            f"resolve to HGNC, above the {MAX_UNRESOLVED_FRACTION:.0%} threshold. "
            f"Open Targets may have changed its target key scheme -- check before "
            f"lowering the bar."
        )

    reports_dir.mkdir(parents=True, exist_ok=True)
    report = reports_dir / "unresolved_mechanism_targets.tsv"
    report.write_text(
        "ensembl_gene_id\n" + "".join(f"{e}\n" for e in sorted(resolver.unresolved_ids or ())),
        encoding="utf-8")

    log(f"  {stats['rows']:,} mechanism rows -> {stats['edges']:,} edges "
        f"({stats['duplicate_edges']:,} duplicates collapsed)")
    log(f"  {stats['single_target_edges']:,} single-target, "
        f"{stats['group_target_edges']:,} family/complex-membership")
    log(f"  {stats['rows_without_targets']:,} rows had no gene target")
    log(f"  {resolver.summary()}; {len(resolver.unresolved_ids or ())} distinct "
        f"unresolved id(s) -> {report}")
    log(f"  {stats['genes']:,} gene nodes, {stats['triples']:,} triples -> {out_path}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", default=DEFAULT_RELEASE)
    parser.add_argument("--indir", type=Path, default=None)
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--hgnc", type=Path,
                        help="Pinned HGNC complete set (default: <indir>/hgnc_complete_set.txt)")
    parser.add_argument("--manifest-dir", type=Path, default=hgnc.MANIFEST_DIR,
                        help="Directory containing <release>-hgnc.json")
    args = parser.parse_args()

    indir = args.indir or Path("opentargets/input") / args.release
    workdir = args.workdir or Path("opentargets") / args.release / "data"
    log(f"Transforming drug_mechanism_of_action from {indir}")
    hgnc_path = args.hgnc or indir / hgnc.HGNC_FILENAME
    hgnc.verify(hgnc_path, hgnc.read_pin(args.release, args.manifest_dir))
    resolver = HgncResolver.from_file(hgnc_path)
    log(f"  HGNC crosswalk: {len(resolver.ensembl_to_hgnc):,} Ensembl ids")
    transform(indir, workdir / "rdf" / "mechanisms.ttl", resolver, workdir / "reports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
