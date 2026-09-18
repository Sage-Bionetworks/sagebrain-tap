"""Replace an Open Targets release graph in Oxigraph and write VoID provenance.

The three Turtle parts go into ``urn:sagebrain:opentargets:<release>``; ``void.ttl``
goes into the default graph, as with Reactome. Reloading replaces only that
release's graph, so releases can sit side by side.

The VoID block carries the caveats a consumer cannot recover from the triples: what
``maxClinicalStage`` does and does not mean, what a family-level mechanism edge
claims, and which pinned dataset is deliberately not projected yet. Those notes are
the difference between a layer that is used correctly and one that is quoted out of
context, so they live in the graph rather than only in a README.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from shared.oxigraph import load_endpoint, load_oxigraph

from .common import (
    DEFAULT_RELEASE,
    IngestError,
    TurtleWriter,
    iri,
    literal,
    log,
    release_graph,
    typed_literal,
)

TTL_PARTS = ("molecules.ttl", "mechanisms.ttl", "indications.ttl")

SCOPE_NOTE = (
    "Open Targets Platform drug layer: ChEMBL molecules with their name/synonym/"
    "trade-name index, mechanism-of-action edges to genes, and clinical indications "
    "with the maximum clinical stage reached. Target-disease association SCORES are "
    "deliberately not ingested -- they are opinionated composites, useful for ranking "
    "and wrong to treat as evidence. clinical_report is pinned and layout-gated but "
    "not projected: indication edges carry a report COUNT instead, and the reports "
    "themselves (trials, labels, stop reasons) are a layer of their own."
)

STAGE_NOTE = (
    "sagebrain:maxClinicalStage is a property of a drug-disease EDGE, not of a drug. "
    "It is the maximum over the clinical reports behind that one pair, so a drug that "
    "failed phase 3 for one disease and was approved for another carries both values "
    "on different edges. Never quote a stage without its indication. The separate "
    "sagebrain:maximumClinicalStage on a molecule is the maximum over ALL of its "
    "indications and says nothing about any particular disease. Approval facts are "
    "for specific indications and populations and are not treatment guidance."
)

PREDICATE_NOTE = (
    "Indication edges use biolink:treats_or_applied_or_studied_to_treat, not "
    "biolink:treats: most rows are trials rather than approvals, and a phase-1 trial "
    "is a compound being studied for a disease, not one that treats it."
)

TARGET_TYPE_NOTE = (
    "sagebrain:targetType decides what a mechanism edge claims. For 'single protein' "
    "the edge is a drug-to-target pair. For 'protein family', 'protein complex', "
    "'selectivity group' and the rest, ChEMBL's target is a named GROUP and the edge "
    "asserts membership of it -- trametinib's 'MEK1/2 inhibitor' row yields an edge to "
    "MAP2K1 and one to MAP2K2 from a single claim about the pair. At 26.06 these "
    "group-membership edges outnumber single-protein edges roughly two to one, so a "
    "query that counts drug-target pairs without filtering on targetType will "
    "substantially over-count."
)

GENE_KEY_NOTE = (
    "Genes are HGNC-keyed so they are one kind of node across this repo and join "
    "Reactome's gene nodes by IRI with no mapping table. Open Targets keys targets on "
    "Ensembl; the id it used is kept on each edge as biolink:original_object."
)


def count_triples(paths: list[Path]) -> int:
    """Count emitted triples by counting statement terminators.

    The TurtleWriter emits one predicate-object pair per line, so a line ending in
    ' ;' or ' .' is exactly one triple. Cheaper than reparsing, and this is a
    metadata figure rather than an acceptance check -- acceptance counts the loaded
    graph and compares the two.
    """
    total = 0
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.rstrip()
                if stripped.startswith(("@prefix", "#")) or not stripped:
                    continue
                if stripped.endswith((";", ".")):
                    total += 1
    return total


def build_void(release: str, metadata: dict, triples: int, run_date: str,
               out_path: Path) -> None:
    graph = release_graph(release)
    activity = iri(f"urn:sagebrain:ingest:opentargets:{run_date}")
    ftp_base = metadata.get("ftp_base", "")
    anchor = (metadata.get("provenance_anchor") or {}).get("sha1", "")
    published = metadata.get("published", "")

    pairs = [
        ("a", "void:Dataset"),
        ("rdfs:label", literal(f"Open Targets Platform {release} drug layer "
                               "-- SageBrain TAP ingest")),
        ("pav:version", literal(release)),
        ("dcterms:license", iri("https://creativecommons.org/publicdomain/zero/1.0/")),
        ("void:triples", str(triples)),
        ("dcterms:description", literal(SCOPE_NOTE)),
        ("rdfs:comment", literal(STAGE_NOTE)),
        ("prov:wasGeneratedBy", activity),
    ]
    if ftp_base:
        pairs.insert(3, ("dcterms:source", iri(f"{ftp_base}/output/")))
    if published:
        pairs.append(("dcterms:issued", typed_literal(published, "xsd:date")))
    if metadata.get("citation_pmid"):
        pairs.append(("dcterms:bibliographicCitation",
                      iri(f"https://pubmed.ncbi.nlm.nih.gov/{metadata['citation_pmid']}/")))
    # Each caveat as its own comment: a consumer reading one does not have to read
    # a single wall of text to find the one that applies to their query.
    for note in (PREDICATE_NOTE, TARGET_TYPE_NOTE, GENE_KEY_NOTE):
        pairs.append(("rdfs:comment", literal(note)))
    if not anchor:
        log("WARNING: no provenance anchor in release.json -- VoID will not record "
            "which bytes this graph was built from. Run download_sources.py.")
    else:
        pairs.append(("sagebrain:sourceIntegrityDigest", literal(f"sha1:{anchor}")))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        writer = TurtleWriter(handle)
        writer.comment("VoID provenance; load into the default graph.")
        writer.statements(iri(graph), pairs)
        writer.statements(activity, [
            ("a", "prov:Activity"),
            ("rdfs:label", literal(f"Open Targets {release} drug-layer ingest, {run_date}")),
            ("prov:used", iri(f"{ftp_base}/output/") if ftp_base
             else literal("Open Targets Platform FTP")),
            ("prov:endedAtTime", typed_literal(run_date, "xsd:date")),
        ])
    log(f"Wrote VoID metadata -> {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", default=DEFAULT_RELEASE)
    parser.add_argument("--ttl-dir", type=Path, default=None,
                        help="Default: opentargets/<release>/data/rdf")
    parser.add_argument("--release-json", type=Path, default=None,
                        help="release.json from download_sources.py (provenance)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--store", type=Path, help="Local Oxigraph store path")
    target.add_argument("--endpoint", help="Graph Store URL, e.g. http://localhost:7011/store")
    parser.add_argument("--run-date", default=date.today().isoformat())
    parser.add_argument("--void-only", action="store_true", help="Only write void.ttl")
    args = parser.parse_args()

    ttl_dir = args.ttl_dir or Path("opentargets") / args.release / "data" / "rdf"
    release_json = args.release_json or Path("opentargets/input") / args.release / "release.json"

    parts = [ttl_dir / name for name in TTL_PARTS]
    missing = [p.name for p in parts if not p.exists()]
    if missing:
        raise IngestError(
            f"Missing Turtle parts: {', '.join(missing)}. Run the transforms first "
            f"(python -m opentargets.pipeline --release {args.release})."
        )

    metadata: dict = {}
    if release_json.exists():
        metadata = json.loads(release_json.read_text(encoding="utf-8"))
    else:
        log(f"WARNING: no {release_json} -- VoID will carry no source or digest. "
            "The ingest will not be traceable to a release. Run download_sources.py.")

    triples = count_triples(parts)
    void_path = ttl_dir / "void.ttl"
    build_void(args.release, metadata, triples, args.run_date, void_path)
    if args.void_only:
        return 0

    graph = release_graph(args.release)
    if args.endpoint:
        load_endpoint(args.endpoint, graph, parts, void_path)
    else:
        store_path = args.store or (ttl_dir.parent / "store")
        load_oxigraph(store_path, graph, parts, void_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
