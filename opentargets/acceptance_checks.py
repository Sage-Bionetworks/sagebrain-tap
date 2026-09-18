"""Structural acceptance checks for a loaded Open Targets release graph.

Reactome's contract, adapted: structural errors fail; the model-term review is a
warning. Each check is a SPARQL query against the loaded graph rather than a
re-read of the Turtle, so what is verified is what a consumer will actually query.

Check 9 is this ingest's equivalent of Reactome's "NF1 across hierarchy levels":
a small set of facts a correct release must contain, chosen because they are the
ones downstream work depends on. If selumetinib stops being APPROVED for plexiform
neurofibroma, either the release changed something real or this ingest broke, and
both are worth stopping for.

Usage:
    python -m opentargets.acceptance_checks --release 26.06
    python -m opentargets.acceptance_checks --release 26.06 --endpoint http://localhost:7011/query
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from shared import model_terms
from shared.oxigraph import GraphClient

from .common import (
    ACTION_TYPES,
    CLINICAL_STAGES,
    DEFAULT_RELEASE,
    DRUG_TYPES,
    TARGET_TYPES,
    log,
    release_graph,
)

PREFIXES = """
PREFIX biolink: <https://w3id.org/biolink/vocab/>
PREFIX sagebrain: <https://w3id.org/synapse/sagebrain#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX void: <http://rdfs.org/ns/void#>
"""

#: Facts a correct 26.06 ingest must contain (check 9). Compound label, the
#: mechanism target it must reach, and an indication it must carry.
DEMO_ANCHORS = [
    ("trametinib", "MAP2K1", None, None),
    ("trametinib", "MAP2K2", "EFO_0000658", "PHASE_2"),
    ("tno155", "PTPN11", None, None),
    ("ribociclib", "CDK4", None, None),
    ("ribociclib", "CDK6", None, None),
    ("selumetinib", "MAP2K1", "EFO_0000658", "APPROVAL"),
    ("mirdametinib", "MAP2K2", "EFO_0000658", "APPROVAL"),
    ("selumetinib", "MAP2K2", "MONDO_0017827", "PHASE_2"),
]

TRIPLE_RANGE = (500_000, 3_000_000)


@dataclass
class Check:
    number: int
    name: str
    passed: bool
    detail: str
    fatal: bool = True
    lines: list[str] = field(default_factory=list)


def _one(client: GraphClient, query: str) -> dict:
    rows = client.select(PREFIXES + query)["results"]["bindings"]
    return rows[0] if rows else {}


def _values(client: GraphClient, query: str, variable: str) -> list[str]:
    return [row[variable]["value"]
            for row in client.select(PREFIXES + query)["results"]["bindings"]
            if variable in row]


def _count(client: GraphClient, graph: str, where: str) -> int:
    row = _one(client, f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{graph}> {{ {where} }} }}")
    return int(row["n"]["value"]) if row else 0


def _distinct(client: GraphClient, graph: str, variable: str, where: str) -> int:
    row = _one(client, f"SELECT (COUNT(DISTINCT ?{variable}) AS ?n) "
                       f"WHERE {{ GRAPH <{graph}> {{ {where} }} }}")
    return int(row["n"]["value"]) if row else 0


def run_checks(client: GraphClient, graph: str, release: str,
               default_client: GraphClient | None = None) -> list[Check]:
    checks: list[Check] = []

    # 1 -- every compound is typed and labelled.
    unlabelled = _count(client, graph, """
        ?c sagebrain:drugType ?t . FILTER NOT EXISTS { ?c rdfs:label ?l }""")
    untyped = _count(client, graph, """
        ?c sagebrain:drugType ?t . FILTER NOT EXISTS { ?c a ?class }""")
    compounds = _distinct(client, graph, "c", "?c sagebrain:drugType ?t")
    checks.append(Check(1, "Compounds typed and labelled",
                        unlabelled == 0 and untyped == 0,
                        f"{compounds:,} compounds; {unlabelled} unlabelled, {untyped} untyped"))

    # 2 -- no mechanism edge points at a gene the ingest did not assert as a node.
    dangling = _count(client, graph, """
        ?a a biolink:ChemicalToGeneAssociation ; biolink:object ?g .
        FILTER NOT EXISTS { ?g a biolink:Gene }""")
    edges = _count(client, graph, "?a a biolink:ChemicalToGeneAssociation")
    checks.append(Check(2, "Mechanism edges resolve to gene nodes", dangling == 0,
                        f"{edges:,} mechanism edges, {dangling} with no typed gene node"))

    # 3 -- same for the disease side.
    dangling_disease = _count(client, graph, """
        ?a a biolink:ChemicalToDiseaseOrPhenotypicFeatureAssociation ; biolink:object ?d .
        FILTER NOT EXISTS {
          { ?d a biolink:Disease } UNION { ?d a biolink:PhenotypicFeature }
          UNION { ?d a biolink:DiseaseOrPhenotypicFeature } }""")
    indications = _count(
        client, graph, "?a a biolink:ChemicalToDiseaseOrPhenotypicFeatureAssociation")
    checks.append(Check(3, "Indication edges resolve to disease/phenotype nodes",
                        dangling_disease == 0,
                        f"{indications:,} indication edges, {dangling_disease} with no "
                        f"typed node"))

    # 4-6 -- controlled vocabularies, verified in the GRAPH rather than the source.
    for number, name, predicate, allowed, constant in (
        (4, "Clinical stages", "sagebrain:maxClinicalStage",
         frozenset(CLINICAL_STAGES), "CLINICAL_STAGES"),
        (5, "Action types", "sagebrain:actionType", ACTION_TYPES, "ACTION_TYPES"),
        (6, "Target types", "sagebrain:targetType", TARGET_TYPES, "TARGET_TYPES"),
    ):
        observed = set(_values(client, f"""
            SELECT DISTINCT ?v WHERE {{ GRAPH <{graph}> {{ ?s {predicate} ?v }} }}""", "v"))
        unknown = sorted(observed - allowed)
        checks.append(Check(number, name, not unknown,
                            f"{len(observed)} value(s) in the graph, all in {constant}"
                            if not unknown else
                            f"not in {constant}: {', '.join(unknown)}"))

    # 7 -- genes are HGNC-keyed and carry exactly one taxon.
    non_hgnc = _count(client, graph, """
        ?g a biolink:Gene .
        FILTER(!STRSTARTS(STR(?g), "https://identifiers.org/hgnc:"))""")
    multi_taxon = len(_values(client, f"""
        SELECT ?g WHERE {{ GRAPH <{graph}> {{ ?g a biolink:Gene ; biolink:in_taxon ?t }} }}
        GROUP BY ?g HAVING(COUNT(DISTINCT ?t) != 1)""", "g"))
    genes = _distinct(client, graph, "g", "?g a biolink:Gene")
    checks.append(Check(7, "Genes HGNC-keyed with one taxon",
                        non_hgnc == 0 and multi_taxon == 0,
                        f"{genes:,} gene nodes; {non_hgnc} not HGNC-keyed, "
                        f"{multi_taxon} without exactly one taxon"))

    # 8 -- graph size, and the loader's own void:triples assertion agrees with it.
    total = _count(client, graph, "?s ?p ?o")
    # VoID is in the DEFAULT graph, while `client` is scoped to the release graph,
    # so asking `client` for it silently returns nothing and the comparison below
    # degrades to "not checked" while still reporting PASS. Use an unscoped client.
    void_client = default_client or client
    declared_rows = void_client.select(PREFIXES + f"""
        SELECT ?n WHERE {{ <{graph}> void:triples ?n }}""")["results"]["bindings"]
    declared = int(declared_rows[0]["n"]["value"]) if declared_rows else None
    low, high = TRIPLE_RANGE
    size_ok = low <= total <= high
    # The counts are compared but a mismatch is not fatal: void:triples is counted
    # from the Turtle by line, the graph count is after RDF deduplication, so they
    # can differ legitimately when a transform emits the same triple twice.
    agrees = declared is None or abs(declared - total) <= total * 0.01
    checks.append(Check(8, "Release size", size_ok and agrees,
                        f"{total:,} triples (expected {low:,}-{high:,}); "
                        f"void:triples asserts {declared:,}"
                        f"{'' if agrees else ' -- DIFFERS by more than 1%'}"
                        if declared is not None else f"{total:,} triples, no void:triples"))

    # 9 -- the facts downstream work depends on.
    missing: list[str] = []
    for label, symbol, disease, stage in DEMO_ANCHORS:
        found = _count(client, graph, f"""
            ?c skos:altLabel|rdfs:label ?l . FILTER(LCASE(STR(?l)) = "{label}")
            ?m a biolink:ChemicalToGeneAssociation ;
               biolink:subject ?c ; biolink:object ?g .
            ?g rdfs:label "{symbol}" .""")
        if not found:
            missing.append(f"{label} -> {symbol} (mechanism)")
        if disease:
            iri = ("http://www.ebi.ac.uk/efo/" if disease.startswith("EFO")
                   else "http://purl.obolibrary.org/obo/") + disease
            found = _count(client, graph, f"""
                ?c skos:altLabel|rdfs:label ?l . FILTER(LCASE(STR(?l)) = "{label}")
                ?i biolink:subject ?c ; biolink:object <{iri}> ;
                   sagebrain:maxClinicalStage "{stage}" .""")
            if not found:
                missing.append(f"{label} -> {disease} at {stage} (indication)")
    checks.append(Check(9, "Known facts present", not missing,
                        f"all {len(DEMO_ANCHORS)} anchors present" if not missing
                        else f"{len(missing)} missing", lines=missing))

    # 10 -- model terms. A warning: the model repo and this ingest move at
    # different speeds, and so does the network.
    pairs = [(row["p"]["value"], row["n"]["value"]) for row in client.select(
        PREFIXES + f"""SELECT ?p (COUNT(*) AS ?n)
        WHERE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }} GROUP BY ?p""")["results"]["bindings"]]
    review = model_terms.review(model_terms.counts_from_iris(pairs))
    checks.append(Check(10, "Model terms defined in sagebrain-model", review.passed,
                        review.detail, fatal=False, lines=review.lines()))
    return checks


def render(release: str, checks: list[Check]) -> str:
    failures = [c for c in checks if not c.passed and c.fatal]
    lines = [f"# Open Targets {release} acceptance report", "",
             "Generated by `python -m opentargets.acceptance_checks`.", "",
             "| # | Check | Result | Detail |", "| --- | --- | --- | --- |"]
    for check in checks:
        mark = "PASS" if check.passed else ("FAIL" if check.fatal else "WARN")
        lines.append(f"| {check.number} | {check.name} | {mark} | {check.detail} |")
    lines.append("")
    for check in checks:
        if check.lines:
            lines += [f"### {check.number}. {check.name}", ""]
            lines += [f"- {line}" for line in check.lines]
            lines.append("")
    lines += ["## Result", "",
              "**FAILED**" if failures else "**PASSED** — no structural check failed.", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", default=DEFAULT_RELEASE)
    parser.add_argument("--store", type=Path, default=None,
                        help="Local Oxigraph store (default: opentargets/<release>/data/store)")
    parser.add_argument("--endpoint", default=None, help="SPARQL query URL instead")
    parser.add_argument("--manifest-dir", type=Path, default=Path("opentargets/manifests"))
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    graph = release_graph(args.release)
    store = args.store or Path("opentargets") / args.release / "data" / "store"
    client = GraphClient(store=None if args.endpoint else store,
                         endpoint=args.endpoint, graphs=[graph])
    # Unscoped, for the default graph where VoID lives.
    default_client = GraphClient(store=None if args.endpoint else store,
                                 endpoint=args.endpoint)

    log(f"Acceptance checks for <{graph}>")
    checks = run_checks(client, graph, args.release, default_client)
    for check in checks:
        mark = "PASS" if check.passed else ("FAIL" if check.fatal else "WARN")
        log(f"  {check.number:>2}. [{mark}] {check.name}: {check.detail}")
        for line in check.lines:
            log(f"        {line}")

    report_path = args.report or args.manifest_dir / f"{args.release}-acceptance.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render(args.release, checks), encoding="utf-8")
    log(f"Wrote {report_path}")

    failures = [c for c in checks if not c.passed and c.fatal]
    if failures:
        log(f"\n{len(failures)} structural check(s) FAILED.")
        return 1
    log("\nAcceptance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
