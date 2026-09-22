"""Map UniProt accessions to HGNC genes and write participation edges.

Two files, one set of facts. associations.ttl reifies every (gene, pathway,
evidence, accession) row so the evidence and the originating accession have
somewhere to live; participation.ttl carries the distinct (gene, pathway) pairs
as plain sagebrain:participates_in edges, which is the form a traversal can
follow. Both are derived from the same in-memory set in one pass, so they
cannot drift, and acceptance check 9 re-proves that against the loaded graph.

_All_Levels already includes ancestors: do not propagate again. Retain the full
accession (including isoform) and TAS/IEA evidence on each association. Report
unmapped accessions in ../reports/unmapped_accessions.tsv; fail above 10%."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

from .common import (
    HUMAN,
    REACTOME_SOURCE,
    IngestError,
    SpeciesFilter,
    TurtleWriter,
    base_accession,
    eco_curie_for_evidence,
    hgnc_curie,
    literal,
    log,
    pathway_curie,
    read_tsv,
    uniprot_curie,
)


#: The value of biolink:predicate on the reified association.
PREDICATE = "biolink:participates_in"

#: The plain traversal edge. sagebrain:participates_in is a ratified model term
#: -- rdfs:subPropertyOf biolink:participates_in, rdfs:domain biolink:Gene,
#: rdfs:range biolink:Pathway -- so this is the shared model's own Gene ->
#: Pathway connection, not a term this ingest had to mint.
DIRECT_PREDICATE = "sagebrain:participates_in"


def load_hgnc(path: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Build UniProt accession -> [HGNC ID] and HGNC ID -> symbol.

    A handful of accessions map to more than one HGNC gene (readthrough loci,
    paralogous entries sharing a reviewed accession). Those fan out to one
    association per gene: picking one arbitrarily would be a silent, unrecorded
    choice, and dropping them loses real curation.
    """
    if not path.exists():
        raise IngestError(
            f"Missing {path}. The HGNC complete set provides the UniProt -> HGNC hop; "
            "fetch it with download_sources.py (or pass --skip-hgnc there only if you "
            "have supplied it another way)."
        )
    accession_to_hgnc: dict[str, list[str]] = defaultdict(list)
    symbols: dict[str, str] = {}
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for column in ("hgnc_id", "uniprot_ids", "symbol"):
            if column not in (reader.fieldnames or []):
                raise IngestError(
                    f"{path.name} has no {column!r} column; the HGNC layout changed."
                )
        for row in reader:
            hgnc_id = (row.get("hgnc_id") or "").strip()
            if not hgnc_id:
                continue
            symbols[hgnc_id] = (row.get("symbol") or "").strip()
            for accession in (row.get("uniprot_ids") or "").split("|"):
                accession = accession.strip()
                if accession:
                    accession_to_hgnc[accession].append(hgnc_id)
    return dict(accession_to_hgnc), symbols


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--indir", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--interim-dir", type=Path, default=None,
                        help="Where pathway_ids.txt is read from (default: a sibling "
                             "'interim/' next to --outdir)")
    parser.add_argument("--reports-dir", type=Path, default=None,
                        help="Where unmapped_accessions.tsv goes (default: a sibling "
                             "'reports/' next to --outdir)")
    parser.add_argument("--species", default=HUMAN)
    parser.add_argument("--hgnc", type=Path, default=None,
                        help="Path to hgnc_complete_set.txt (default: <indir>/hgnc_complete_set.txt)")
    parser.add_argument("--max-unmapped-fraction", type=float, default=0.10,
                        help="Fail if more than this fraction of in-scope rows cannot be "
                             "resolved to HGNC (default: 0.10)")
    args = parser.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)
    interim_dir = args.interim_dir or (args.outdir.parent / "interim")
    reports_dir = args.reports_dir or (args.outdir.parent / "reports")
    reports_dir.mkdir(parents=True, exist_ok=True)

    ids_path = interim_dir / "pathway_ids.txt"
    if not ids_path.exists():
        raise IngestError(
            f"Missing {ids_path}. Run transform_pathways.py first -- the association "
            "transform filters against the pathway ID set it writes, so that all three "
            "transforms agree on scope."
        )
    in_scope = {line.strip() for line in ids_path.read_text().splitlines() if line.strip()}
    log(f"Loaded {len(in_scope):,} in-scope pathway IDs")

    hgnc_path = args.hgnc or (args.indir / "hgnc_complete_set.txt")
    log("Loading HGNC mapping ...")
    accession_to_hgnc, symbols = load_hgnc(hgnc_path)
    log(f"  {len(accession_to_hgnc):,} UniProt accessions mapped to HGNC genes")

    species_filter = SpeciesFilter(species_name=args.species)
    source = args.indir / "UniProt2Reactome_All_Levels.txt"

    rows_in_scope = 0
    unresolved_rows = 0
    off_scope_pathway_rows = 0
    fanned_out = 0
    unmapped: dict[str, dict] = {}
    evidence_counts: dict[str, int] = defaultdict(int)
    genes_seen: set[str] = set()
    facts: set[tuple[str, str, str, str]] = set()

    log("Streaming associations ...")
    for row in read_tsv(
        source, expected_fields=6, has_header=False, filename_must_contain="_All_Levels"
    ):
        if not species_filter.keep_row(row, species_column=6):
            continue
        rows_in_scope += 1

        accession, stable_id, _url, _event, code, _species = (value.strip() for value in row)

        if stable_id not in in_scope:
            # The pathway file and the association file are filtered by the same
            # species name, so this should be empty. If it is not, the two files
            # disagree and that is worth knowing about.
            off_scope_pathway_rows += 1
            continue

        eco = eco_curie_for_evidence(code)
        evidence_counts[code] += 1

        hgnc_ids = accession_to_hgnc.get(base_accession(accession))
        if not hgnc_ids:
            unresolved_rows += 1
            entry = unmapped.setdefault(
                accession, {"rows": 0, "example_pathway": stable_id, "evidence": code}
            )
            entry["rows"] += 1
            continue

        if len(hgnc_ids) > 1:
            fanned_out += 1
        for hgnc_id in hgnc_ids:
            genes_seen.add(hgnc_id)
            facts.add((hgnc_id, stable_id, eco, accession))

    log(f"  {species_filter.summary()}")
    log(f"  in-scope rows: {rows_in_scope:,}")
    log(f"  distinct associations: {len(facts):,}")
    log(f"  genes: {len(genes_seen):,}")
    log("  evidence: " + ", ".join(f"{c}={n:,}" for c, n in sorted(evidence_counts.items())))
    if fanned_out:
        log(f"  rows fanned out across multiple HGNC genes: {fanned_out:,}")
    if off_scope_pathway_rows:
        log(f"  WARNING: {off_scope_pathway_rows:,} in-scope rows referenced a pathway "
            "absent from the pathway file")

    if rows_in_scope == 0:
        raise IngestError(f"No association rows matched species {args.species!r}.")

    fraction = unresolved_rows / rows_in_scope
    log(f"  unresolved to HGNC: {unresolved_rows:,} rows ({fraction:.2%}), "
        f"{len(unmapped):,} distinct accessions")

    rejects = reports_dir / "unmapped_accessions.tsv"
    with open(rejects, "w", encoding="utf-8") as handle:
        handle.write("uniprot_accession\trows_dropped\texample_pathway\tevidence_code\n")
        for accession in sorted(unmapped):
            entry = unmapped[accession]
            handle.write(
                f"{accession}\t{entry['rows']}\t{entry['example_pathway']}\t{entry['evidence']}\n"
            )
    log(f"  wrote unmapped accessions -> {rejects}")

    if fraction > args.max_unmapped_fraction:
        raise IngestError(
            f"{fraction:.2%} of in-scope rows could not be resolved to an HGNC gene, "
            f"above the {args.max_unmapped_fraction:.2%} threshold. The HGNC file is "
            f"probably stale or truncated. See {rejects}."
        )

    output = args.outdir / "associations.ttl"
    with open(output, "w", encoding="utf-8") as handle:
        writer = TurtleWriter(handle)
        writer.comment(
            "Reactome gene-to-pathway associations.\n"
            "Source file is _All_Levels: already transitively closed up the hierarchy.\n"
            "DO NOT apply transitive closure again.\n"
            "Association counts per pathway are non-independent for the same reason --\n"
            "do not feed raw counts to a hypergeometric test (plan section 5.4).\n"
            "The UniProt accession each association came from is kept on the\n"
            "association as biolink:original_subject, a string.\n"
            f"Scope: {args.species} ({species_filter.taxon_curie}).\n"
            "Generated by reactome/transform_associations.py"
        )

        for hgnc_id in sorted(genes_seen):
            pairs = [("a", "biolink:Gene"), ("biolink:in_taxon", species_filter.taxon_curie)]
            symbol = symbols.get(hgnc_id)
            if symbol:
                pairs.insert(1, ("rdfs:label", literal(symbol)))
            writer.statements(hgnc_curie(hgnc_id), pairs)

        for hgnc_id, stable_id, eco, accession in sorted(facts):
            writer.blank_node(
                [
                    ("a", "biolink:GeneToPathwayAssociation"),
                    ("biolink:subject", hgnc_curie(hgnc_id)),
                    ("biolink:object", pathway_curie(stable_id)),
                    ("biolink:predicate", PREDICATE),
                    ("biolink:has_evidence", eco),
                    ("biolink:primary_knowledge_source", REACTOME_SOURCE),
                    # Biolink's own slot for "what the source called the
                    # subject before we normalised it", so no local term is
                    # needed. A string, because original_subject is an
                    # owl:DatatypeProperty -- which means the accession cannot
                    # be traversed. That is the right trade while nothing in the
                    # graph describes UniProt entities: an IRI in object
                    # position with no typed node behind it is not a join, just
                    # a longer string. If a source that makes statements about
                    # UniProt entities is ingested, the answer is to type those
                    # entities (biolink:Protein) and link them from the gene
                    # with biolink:has_gene_product -- still no local term.
                    ("biolink:original_subject", literal(uniprot_curie(accession))),
                ]
            )

    association_triples = writer.triples
    log(f"\nWrote {association_triples:,} triples -> {output}")

    # ── the plain traversal edge ──────────────────────────────────────────────
    # Derived from the same `facts` set, in the same run, rather than recomputed
    # from the source: two representations of one fact drift the moment they
    # have two derivations.
    pathways_by_gene: dict[str, set[str]] = defaultdict(set)
    for hgnc_id, stable_id, _eco, _accession in facts:
        pathways_by_gene[hgnc_id].add(stable_id)
    edges = sum(len(p) for p in pathways_by_gene.values())

    participation = args.outdir / "participation.ttl"
    with open(participation, "w", encoding="utf-8") as handle:
        writer = TurtleWriter(handle)
        writer.comment(
            "Gene -> Pathway traversal edges: the distinct (gene, pathway) pairs of\n"
            "associations.ttl, as plain edges.\n"
            "\n"
            "sagebrain:participates_in is an owl:ObjectProperty, so a gene's pathways\n"
            "and their ancestors are one property path --\n"
            "  ?gene sagebrain:participates_in/biolink:part_of* ?ancestor\n"
            "-- where the reified form needs a two-triple join through a blank node for\n"
            "every hop. Nothing in this stack reasons over rdfs:subPropertyOf, so the\n"
            "biolink:participates_in superproperty is NOT materialised here; the\n"
            "associations already carry it as the value of biolink:predicate.\n"
            "\n"
            "THESE EDGES ARE A UNION OVER EVIDENCE AND OVER SOURCE ACCESSIONS. One pair\n"
            "can come from several accessions and carry TAS and IEA at once, and an edge\n"
            "cannot say which: a substantial minority of pairs are IEA-only, i.e.\n"
            "orthology-projected rather than curated. associations.ttl stays\n"
            "authoritative for evidence, for the originating UniProt accession and for\n"
            "the knowledge source. Anything that cares must join through it.\n"
            "\n"
            "The pre-propagation warning applies here with less excuse than anywhere\n"
            "else, because a plain edge is what invites the wrong query. _All_Levels is\n"
            "already transitively closed: a gene is asserted against every ancestor of\n"
            "the pathway it participates in. Per-pathway counts over these edges are NOT\n"
            "independent categories -- do not feed them to a hypergeometric test -- and\n"
            "following biolink:part_of from one re-walks a closure already applied.\n"
            f"Scope: {args.species} ({species_filter.taxon_curie}).\n"
            "Generated by reactome/transform_associations.py"
        )
        for hgnc_id in sorted(pathways_by_gene):
            writer.statements(
                hgnc_curie(hgnc_id),
                [(DIRECT_PREDICATE, pathway_curie(stable_id))
                 for stable_id in sorted(pathways_by_gene[hgnc_id])],
            )

    log(f"Wrote {writer.triples:,} triples -> {participation}")
    log(f"  {edges:,} distinct (gene, pathway) pairs from "
        f"{len(facts):,} associations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
