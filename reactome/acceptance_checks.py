"""Check species, identifiers, hierarchy, evidence, NF1 and release size.

The optional Content Service comparison (--round-trip) needs network access;
differences or an unavailable service produce warnings, not ingest failures."""

from __future__ import annotations

import argparse
import json
import random
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from shared.model_terms import MODEL_URL, counts_from_iris, review

from .common import EVIDENCE_CODE_TO_ECO, PREFIXES, IngestError, expand, log


PREFIX_BLOCK = "\n".join(f"PREFIX {p}: <{u}>" for p, u in sorted(PREFIXES.items()))

NF1_HGNC = "HGNC:7765"
NF1_UNIPROT = "P21359"
RAF_MAP_KINASE_CASCADE = "R-HSA-5673001"

CONTENT_SERVICE = "https://reactome.org/ContentService"


@dataclass
class Result:
    name: str
    passed: bool
    detail: str
    critical: bool = True

    @property
    def status(self) -> str:
        if self.passed:
            return "PASS"
        return "FAIL" if self.critical else "WARN"


class GraphQuerier:
    """Runs SPARQL against either a local Oxigraph store or a remote endpoint."""

    def __init__(self, graph: str, store: Path | None = None, endpoint: str | None = None):
        self.graph = graph
        self.endpoint = endpoint
        self.store = None
        if endpoint is None:
            try:
                import pyoxigraph
            except ImportError as error:  # pragma: no cover
                raise IngestError("pyoxigraph is required unless --endpoint is given.") from error
            if store is None or not store.exists():
                raise IngestError(f"No Oxigraph store at {store}. Run load_graph.py first.")
            self.store = pyoxigraph.Store.read_only(str(store))

    def select(self, body: str) -> list[dict[str, str]]:
        query = f"{PREFIX_BLOCK}\n{body}"
        if self.store is not None:
            solutions = self.store.query(query)
            names = [str(v).lstrip("?") for v in solutions.variables]
            rows = []
            for solution in solutions:
                row = {}
                for index, name in enumerate(names):
                    term = solution[index]
                    row[name] = term.value if term is not None else None
                rows.append(row)
            return rows
        request = urllib.request.Request(
            self.endpoint,
            data=urllib.parse.urlencode({"query": query}).encode(),
            headers={"Accept": "application/sparql-results+json"},
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            payload = json.load(response)
        return [
            {k: v["value"] for k, v in binding.items()}
            for binding in payload["results"]["bindings"]
        ]


def check_species_assertion(q: GraphQuerier) -> Result:
    """Plan appendix: should return zero rows. Run this before anything else."""
    rows = q.select(f"""
        SELECT ?node ?taxon WHERE {{
          GRAPH <{q.graph}> {{
            ?node biolink:in_taxon ?taxon .
            FILTER (?taxon != NCBITaxon:9606)
          }}
        }} LIMIT 20
    """)
    if rows:
        sample = ", ".join(f"{r['node']} -> {r['taxon']}" for r in rows[:3])
        return Result(
            "1. Species assertion (taxon == 9606)", False,
            f"{len(rows)}+ nodes carry a non-human taxon. The species filter did not "
            f"fire on at least one file. Sample: {sample}",
        )
    return Result("1. Species assertion (taxon == 9606)", True, "zero rows, as required")


def check_stable_ids(q: GraphQuerier) -> Result:
    """Belt-and-braces companion: catches a row whose taxon was assigned by default."""
    rows = q.select(f"""
        SELECT ?pathway WHERE {{
          GRAPH <{q.graph}> {{
            ?pathway a biolink:Pathway .
            FILTER (!CONTAINS(STR(?pathway), "R-HSA-"))
          }}
        }} LIMIT 20
    """)
    if rows:
        return Result(
            "2. No non-R-HSA stable IDs", False,
            f"{len(rows)}+ pathways have a non-human stable ID, e.g. {rows[0]['pathway']}",
        )
    return Result("2. No non-R-HSA stable IDs", True, "zero rows, as required")


def check_taxon_cardinality(q: GraphQuerier) -> Result:
    rows = q.select(f"""
        SELECT ?node (COUNT(?taxon) AS ?n) WHERE {{
          GRAPH <{q.graph}> {{
            ?node a ?cls .
            VALUES ?cls {{ biolink:Pathway biolink:Gene }}
            OPTIONAL {{ ?node biolink:in_taxon ?taxon }}
          }}
        }}
        GROUP BY ?node
        HAVING (COUNT(?taxon) != 1)
        LIMIT 20
    """)
    if rows:
        return Result(
            "3. Exactly one in_taxon per node", False,
            f"{len(rows)}+ nodes have a taxon count != 1, e.g. "
            f"{rows[0]['node']} has {rows[0]['n']}",
        )
    return Result("3. Exactly one in_taxon per node", True, "every entity node has exactly one")


def check_pathway_count(q: GraphQuerier, expected: int | None, tolerance: float) -> Result:
    rows = q.select(f"""
        SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE {{
          GRAPH <{q.graph}> {{ ?p a biolink:Pathway }}
        }}
    """)
    count = int(rows[0]["n"])
    if expected is None:
        return Result(
            "4. Pathway count", True,
            f"{count:,} human pathways (no --expected-pathways given to compare against; "
            "check reactome.org/about/news for the release figure)",
            critical=False,
        )
    drift = abs(count - expected) / expected
    if count > 10_000:
        return Result(
            "4. Pathway count", False,
            f"{count:,} pathways -- a count in the tens of thousands is the signature of "
            "a species filter that did not fire, not of Reactome growing",
        )
    if drift > tolerance:
        return Result(
            "4. Pathway count", False,
            f"{count:,} vs expected {expected:,} ({drift:.1%} drift, tolerance {tolerance:.0%})",
        )
    return Result(
        "4. Pathway count", True,
        f"{count:,} vs expected {expected:,} ({drift:.1%} drift)",
    )


def check_acyclic(q: GraphQuerier) -> Result:
    """Cycle detection over biolink:part_of.

    Done in Python over the edge list rather than with a ``part_of+`` property
    path: the hierarchy is small, an explicit walk can name the offending cycle,
    and an unbounded path query with both ends unbound is needlessly expensive.
    """
    rows = q.select(f"""
        SELECT ?child ?parent WHERE {{
          GRAPH <{q.graph}> {{ ?child biolink:part_of ?parent }}
        }}
    """)
    parents: dict[str, list[str]] = {}
    for row in rows:
        parents.setdefault(row["child"], []).append(row["parent"])

    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {}
    cycle: list[str] = []

    def visit(node: str, stack: list[str]) -> bool:
        colour[node] = GREY
        stack.append(node)
        for parent in parents.get(node, []):
            state = colour.get(parent, WHITE)
            if state == GREY:
                cycle.extend(stack[stack.index(parent):] + [parent])
                return True
            if state == WHITE and visit(parent, stack):
                return True
        stack.pop()
        colour[node] = BLACK
        return False

    sys.setrecursionlimit(max(10_000, len(parents) * 4))
    for node in parents:
        if colour.get(node, WHITE) == WHITE and visit(node, []):
            short = " -> ".join(c.rsplit("/", 1)[-1] for c in cycle)
            return Result(
                "5. Hierarchy is acyclic", False,
                f"cycle found in biolink:part_of: {short}. Reactome's own hierarchy is a "
                "DAG, so this is a transform bug.",
            )
    return Result(
        "5. Hierarchy is acyclic", True,
        f"{len(rows):,} part_of edges, no cycles",
    )


def check_no_orphans(q: GraphQuerier) -> Result:
    rows = q.select(f"""
        SELECT ?node WHERE {{
          GRAPH <{q.graph}> {{
            {{ ?node biolink:part_of ?other }} UNION {{ ?other biolink:part_of ?node }}
            FILTER NOT EXISTS {{ ?node a biolink:Pathway }}
          }}
        }} LIMIT 20
    """)
    if rows:
        return Result(
            "6. No dangling hierarchy endpoints", False,
            f"{len(rows)}+ hierarchy endpoints are not pathway nodes, e.g. {rows[0]['node']}. "
            "The relation file was filtered inconsistently with the pathway file.",
        )
    return Result(
        "6. No dangling hierarchy endpoints", True,
        "every part_of endpoint resolves to a pathway node",
    )


def check_evidence_codes(q: GraphQuerier) -> Result:
    rows = q.select(f"""
        SELECT DISTINCT ?evidence WHERE {{
          GRAPH <{q.graph}> {{ ?assoc biolink:has_evidence ?evidence }}
        }}
    """)
    found = {r["evidence"] for r in rows}
    allowed = {expand(eco) for eco in EVIDENCE_CODE_TO_ECO.values()}
    unexpected = found - allowed
    missing_slot = q.select(f"""
        SELECT (COUNT(?assoc) AS ?n) WHERE {{
          GRAPH <{q.graph}> {{
            ?assoc a biolink:GeneToPathwayAssociation .
            FILTER NOT EXISTS {{ ?assoc biolink:has_evidence ?e }}
          }}
        }}
    """)
    nulls = int(missing_slot[0]["n"])
    if unexpected or nulls:
        return Result(
            "7. Evidence codes are the mapped set", False,
            f"unexpected codes: {sorted(unexpected) or 'none'}; "
            f"associations with no evidence: {nulls:,}",
        )
    labels = {expand(v): k for k, v in EVIDENCE_CODE_TO_ECO.items()}
    return Result(
        "7. Evidence codes are the mapped set", True,
        "only " + ", ".join(sorted(f"{labels[f]} ({f.rsplit('/', 1)[-1]})" for f in found)),
    )


def check_nf1(q: GraphQuerier) -> Result:
    rows = q.select(f"""
        SELECT ?pathway ?label ?evidence WHERE {{
          GRAPH <{q.graph}> {{
            ?assoc biolink:subject      {NF1_HGNC} ;
                   biolink:object       ?pathway ;
                   biolink:has_evidence ?evidence .
            ?pathway rdfs:label ?label .
          }}
        }}
    """)
    if not rows:
        return Result(
            "8. NF1 spot check", False,
            f"{NF1_HGNC} participates in no pathways -- the UniProt -> HGNC hop is broken",
        )
    labels = {r["label"] for r in rows}
    ids = {r["pathway"].rsplit(":", 1)[-1] for r in rows}
    has_raf = RAF_MAP_KINASE_CASCADE in ids
    ras_related = sorted(l for l in labels if "RAS" in l.upper() or "MAPK" in l.upper()
                         or "MAP kinase" in l)

    # "at multiple hierarchy levels": at least one returned pathway is an
    # ancestor of another returned pathway.
    ancestry = q.select(f"""
        SELECT (COUNT(*) AS ?n) WHERE {{
          GRAPH <{q.graph}> {{
            ?a biolink:subject {NF1_HGNC} ; biolink:object ?child .
            ?b biolink:subject {NF1_HGNC} ; biolink:object ?parent .
            ?child biolink:part_of ?parent .
          }}
        }}
    """)
    nested = int(ancestry[0]["n"])

    problems = []
    if not has_raf:
        problems.append(f"RAF/MAP kinase cascade ({RAF_MAP_KINASE_CASCADE}) absent")
    if not ras_related:
        problems.append("no RAS/MAPK-related pathways")
    if nested == 0:
        problems.append("all hits are at a single hierarchy level (propagation missing)")
    if problems:
        return Result("8. NF1 spot check", False,
                      f"{len(rows)} pathways but: " + "; ".join(problems))
    return Result(
        "8. NF1 spot check", True,
        f"{len(rows)} pathways, {nested} parent/child pairs among them, "
        f"RAF/MAP kinase cascade present; RAS/MAPK-related: {len(ras_related)}",
    )


def check_model_terms(q: GraphQuerier, model_ttl=MODEL_URL) -> Result:
    """Every sagebrain: term in the graph should be defined in the model repo.

    A WARNING, not a failure. This ingest mints a term when it needs one the
    model has not ratified yet -- there is only one model namespace, so there is
    nowhere else to put it -- and the point of the check is that the term cannot
    then be forgotten: it is named in the acceptance report every run, as a
    to-do for sagebrain-model.

    Asked of the loaded graph rather than the Turtle on disk, because what a
    consumer queries is the graph.
    """
    rows = q.select(f"""
        SELECT ?term (COUNT(*) AS ?n) WHERE {{
          GRAPH <{q.graph}> {{
            {{ ?s ?term ?o }} UNION {{ ?s a ?term }}
          }}
        }} GROUP BY ?term
    """)
    result = review(counts_from_iris((r["term"], r["n"]) for r in rows), model_ttl)
    return Result("11. Model terms are defined in sagebrain-model",
                  result.passed, result.detail, critical=False)


def check_round_trip(q: GraphQuerier, sample_size: int, seed: int) -> Result:
    """Compare member accession lists against the Reactome Content Service.

    Compares ``biolink:original_subject`` -- the UniProt accession Reactome
    itself stated -- rather than the HGNC subject: that is the apples-to-apples
    comparison with Reactome's own reference entities, and it isolates ingest
    errors from expected losses in the UniProt -> HGNC hop.
    """
    pathways = q.select(f"""
        SELECT DISTINCT ?pathway WHERE {{
          GRAPH <{q.graph}> {{ ?assoc biolink:object ?pathway }}
        }}
    """)
    if not pathways:
        return Result("9. Content Service round-trip", False, "no pathways with associations")

    ids = sorted(p["pathway"].rsplit(":", 1)[-1] for p in pathways)
    chosen = random.Random(seed).sample(ids, min(sample_size, len(ids)))

    lines = []
    failures = 0
    for stable_id in chosen:
        ours = {
            r["accession"].rsplit(":", 1)[-1]
            for r in q.select(f"""
                SELECT DISTINCT ?accession WHERE {{
                  GRAPH <{q.graph}> {{
                    ?assoc biolink:object REACT:{stable_id} ;
                           biolink:original_subject ?accession .
                  }}
                }}
            """)
        }
        url = f"{CONTENT_SERVICE}/data/participants/{stable_id}/referenceEntities"
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    # The Content Service rejects urllib's default User-Agent.
                    "User-Agent": "sagebrain-reactome-ingest",
                },
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                entities = json.load(response)
        except (urllib.error.URLError, json.JSONDecodeError) as error:
            return Result("9. Content Service round-trip", False,
                          f"skipped -- Content Service unreachable ({error})", critical=False)
        theirs = {
            e["identifier"] for e in entities
            if e.get("databaseName") == "UniProt" and e.get("identifier")
        }
        missing, extra = theirs - ours, ours - theirs
        if missing or extra:
            failures += 1
            lines.append(
                f"{stable_id}: ours={len(ours)} theirs={len(theirs)} "
                f"missing={len(missing)} extra={len(extra)}"
            )
        else:
            lines.append(f"{stable_id}: {len(ours)} accessions, exact match")

    detail = "; ".join(lines)
    if failures:
        return Result(
            "9. Content Service round-trip", False,
            f"{failures}/{len(chosen)} pathways differ. {detail}. Differences of a few "
            "accessions are usually unmapped entries -- check unmapped_accessions.tsv "
            "before treating this as a transform bug.",
            critical=False,
        )
    return Result("9. Content Service round-trip", True, detail)


def check_triple_count(q: GraphQuerier, low: int, high: int) -> Result:
    rows = q.select(f"""
        SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{q.graph}> {{ ?s ?p ?o }} }}
    """)
    count = int(rows[0]["n"])
    if not (low <= count <= high):
        return Result(
            "10. Triple count in range", False,
            f"{count:,} triples, outside the expected {low:,}-{high:,}",
        )
    return Result("10. Triple count in range", True, f"{count:,} triples")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--store", type=Path)
    target.add_argument("--endpoint", help="SPARQL query URL, e.g. http://localhost:7010/query")
    parser.add_argument("--graph", default=None, help="Override the named graph URI")
    parser.add_argument("--expected-pathways", type=int, default=None,
                        help="Human pathway count from the release announcement")
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("--round-trip", type=int, default=5,
                        help="Pathways to compare against the Content Service (0 to skip)")
    parser.add_argument("--seed", type=int, default=97, help="Seed for round-trip sampling")
    parser.add_argument("--triples-low", type=int, default=1_000_000)
    parser.add_argument("--triples-high", type=int, default=4_000_000)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--model-ttl", default=MODEL_URL,
                        help="sagebrain-model ontology to check emitted terms against, "
                             "as a path or URL. Default: the published model on GitHub")
    args = parser.parse_args()

    graph = args.graph or f"urn:sagebrain:reactome:v{args.version.lstrip('v')}"
    querier = GraphQuerier(graph, store=args.store, endpoint=args.endpoint)
    log(f"Running acceptance checks against <{graph}>\n")

    results = [
        check_species_assertion(querier),
        check_stable_ids(querier),
        check_taxon_cardinality(querier),
        check_pathway_count(querier, args.expected_pathways, args.tolerance),
        check_acyclic(querier),
        check_no_orphans(querier),
        check_evidence_codes(querier),
        check_nf1(querier),
    ]
    if args.round_trip:
        results.append(check_round_trip(querier, args.round_trip, args.seed))
    results.append(check_triple_count(querier, args.triples_low, args.triples_high))
    results.append(check_model_terms(querier, args.model_ttl))

    for result in results:
        log(f"[{result.status:4}] {result.name}")
        log(f"        {result.detail}")

    failed = [r for r in results if not r.passed and r.critical]
    warned = [r for r in results if not r.passed and not r.critical]
    log(f"\n{len(results) - len(failed) - len(warned)} passed, "
        f"{len(warned)} warnings, {len(failed)} failed")

    if args.report:
        body = [f"# Reactome v{args.version} -- acceptance checks", "",
                f"Graph: `<{graph}>`", "", "| Check | Status | Detail |", "|---|---|---|"]
        for result in results:
            body.append(f"| {result.name} | **{result.status}** | {result.detail} |")
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text("\n".join(body) + "\n", encoding="utf-8")
        log(f"Wrote report -> {args.report}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
