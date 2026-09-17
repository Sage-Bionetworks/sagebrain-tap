"""Replace a Reactome release graph in Oxigraph and write VoID provenance.

The three core Turtle files go into urn:sagebrain:reactome:v<version>;
void.ttl goes into the default graph. Retired pathways are loaded separately."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .common import IngestError, TurtleWriter, iri, literal, log, typed_literal
from shared.oxigraph import load_endpoint, load_oxigraph


TTL_PARTS = ("pathways.ttl", "associations.ttl", "go_crosswalk.ttl")

PRE_PROPAGATION_NOTE = (
    "Gene-to-pathway associations are derived from Reactome's "
    "UniProt2Reactome_All_Levels.txt, which is already transitively closed up the "
    "pathway hierarchy: a gene is asserted against every ancestor of the pathway it "
    "participates in. Association counts per pathway are therefore NOT independent. "
    "Do not feed raw per-pathway gene counts to a hypergeometric test or any other "
    "enrichment statistic that assumes independent categories; account for the "
    "hierarchy first. Do not apply transitive closure to these associations again."
)

SCOPE_NOTE = (
    "Scope is Homo sapiens (NCBITaxon:9606) only. Reactome curates human pathways "
    "manually; all other species are computationally projected from human via Ensembl "
    "Compara orthology. Non-human pathways are separate nodes with their own stable "
    "IDs, not the same nodes retagged."
)


def graph_uri(version: str) -> str:
    version = version.lstrip("vV")
    if not version.isdecimal():
        raise IngestError("Reactome version must be a release number, e.g. 97")
    return f"urn:sagebrain:reactome:v{version}"


def ingest_activity_uri(run_date: str) -> str:
    return f"urn:sagebrain:ingest:reactome:{run_date}"


def count_triples(paths: list[Path]) -> int:
    """Count emitted triples by counting statement terminators.

    The TurtleWriter emits one predicate-object pair per line, so a line ending
    in ' ;' or ' .' is exactly one triple. Cheaper than reparsing 1M+ triples,
    and this is a metadata figure, not an acceptance check.
    """
    total = 0
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.rstrip()
                if stripped.startswith(("@prefix", "#")) or not stripped:
                    continue
                if stripped.endswith(";") or stripped.endswith("."):
                    total += 1
    return total


def build_void(
    version: str,
    release: dict,
    triples: int,
    run_date: str,
    out_path: Path,
) -> None:
    graph = graph_uri(version)
    doi_url = release.get("doi_url") or f"https://doi.org/{release.get('doi', '')}"
    issued = release.get("publication_date", "")
    # Zenodo publication_date can be YYYY-MM; xsd:date needs a full date.
    if len(issued) == 7:
        issued = f"{issued}-01"
    license_id = (release.get("license_id") or "cc-by-4.0").lower()
    license_url = (
        "https://creativecommons.org/licenses/by/4.0/"
        if license_id.startswith("cc-by-4")
        else f"https://spdx.org/licenses/{license_id}"
    )

    activity = iri(ingest_activity_uri(run_date))
    pairs = [
        ("a", "void:Dataset"),
        ("rdfs:label", literal(f"Reactome {release.get('version', 'v' + version)} "
                               "(Homo sapiens) -- SageBrain Tier 1 ingest")),
        ("dcterms:source", iri(doi_url)),
        ("pav:version", literal(release.get("version", "V" + version).upper())),
        ("dcterms:license", iri(license_url)),
        ("void:triples", str(triples)),
        ("biolink:in_taxon", "NCBITaxon:9606"),
        ("dcterms:description", literal(SCOPE_NOTE)),
        ("rdfs:comment", literal(PRE_PROPAGATION_NOTE)),
        ("prov:wasGeneratedBy", activity),
    ]
    if issued:
        pairs.append(("dcterms:issued", typed_literal(issued, "xsd:date")))
    with out_path.open("w", encoding="utf-8") as handle:
        writer = TurtleWriter(handle)
        writer.comment("VoID provenance; load into the default graph.")
        writer.statements(iri(graph), pairs)
        writer.statements(activity, [
            ("a", "prov:Activity"),
            ("rdfs:label", literal(f"Reactome Tier 1 RDF ingest, {run_date}")),
            ("prov:used", iri(doi_url)),
            ("prov:endedAtTime", typed_literal(run_date, "xsd:date")),
        ])
    log(f"Wrote VoID metadata -> {out_path}")




def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ttl-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-json", type=Path, default=None,
                        help="release.json written by download_sources.py (for the DOI)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--store", type=Path, help="Local Oxigraph store path")
    target.add_argument("--endpoint", help="Graph Store URL, e.g. http://localhost:7010/store")
    parser.add_argument("--run-date", default=date.today().isoformat())
    parser.add_argument("--void-only", action="store_true", help="Only write void.ttl")
    args = parser.parse_args()

    version = args.version.lstrip("vV")
    parts = [args.ttl_dir / name for name in TTL_PARTS]
    missing = [p.name for p in parts if not p.exists()]
    if missing:
        raise IngestError(f"Missing Turtle parts: {', '.join(missing)}. Run the transforms first.")

    release: dict = {}
    if args.release_json and args.release_json.exists():
        release = json.loads(args.release_json.read_text())
    else:
        log("WARNING: no release.json -- VoID will carry no Zenodo DOI. "
            "The ingest will not be citable. Run download_sources.py to record it.")
        release = {"version": f"v{version}", "doi": "", "doi_url": "", "publication_date": ""}

    triples = count_triples(parts)
    void_path = args.ttl_dir / "void.ttl"
    build_void(version, release, triples, args.run_date, void_path)

    if args.void_only:
        return 0

    graph = graph_uri(version)
    if args.endpoint:
        load_endpoint(args.endpoint, graph, parts, void_path)
    else:
        store_path = args.store or (args.ttl_dir.parent / "store")
        load_oxigraph(store_path, graph, parts, void_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
