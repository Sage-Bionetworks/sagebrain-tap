"""Query a Reactome release in a local Oxigraph store or via HTTP.

Canned queries select --version (default: 97). Raw SPARQL uses its own GRAPH
or FROM clauses. Results are returned as TSV, CSV or SPARQL JSON."""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path

from .common import PREFIXES, IngestError
from .load_graph import graph_uri


DEFAULT_ENDPOINT = os.environ.get("OXIGRAPH_ENDPOINT")

# OPTIONAL keeps panel genes with no associations visible as zero-count rows.
DISEASE_GENE_PANELS = {
    "als": ["SOD1", "C9orf72", "FUS", "TARDBP"],
    "alzheimers": ["APP", "PSEN1", "PSEN2", "APOE", "MAPT"],
}

SYMBOL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _symbol(value: str, flag: str) -> str:
    if not SYMBOL_RE.match(value):
        raise argparse.ArgumentTypeError(f"{flag}: invalid gene symbol {value!r}")
    return value


CANNED_QUERIES = {
    "gene-pathway-count": {
        "help": "How many pathways is a gene part of? Requires --gene.",
        "needs_gene": True,
        "query": """\
SELECT ?symbol (COUNT(DISTINCT ?pathway) AS ?pathway_count) WHERE {{
  ?gene a biolink:Gene ; rdfs:label "{gene}" .
  BIND("{gene}" AS ?symbol)
  ?assoc biolink:subject ?gene ; biolink:object ?pathway .
}} GROUP BY ?symbol""",
    },
    "gene-pathways": {
        "help": "The actual pathway list (label + evidence) for a gene. Requires --gene.",
        "needs_gene": True,
        "query": """\
SELECT ?label ?evidence WHERE {{
  ?gene a biolink:Gene ; rdfs:label "{gene}" .
  ?assoc biolink:subject ?gene ; biolink:object ?pathway ; biolink:has_evidence ?evidence .
  ?pathway rdfs:label ?label .
}} ORDER BY ?label""",
    },
    "disease-gene-panel": {
        "help": "Pathway counts for a curated ALS or Alzheimer's gene panel. --panel als|alzheimers (default: als).",
        "needs_panel": True,
        "query": """\
SELECT ?symbol (COUNT(DISTINCT ?pathway) AS ?pathway_count) WHERE {{
  VALUES ?symbol {{ {symbols} }}
  OPTIONAL {{
    ?gene a biolink:Gene ; rdfs:label ?symbol .
    OPTIONAL {{ ?assoc biolink:subject ?gene ; biolink:object ?pathway }}
  }}
}} GROUP BY ?symbol ORDER BY DESC(?pathway_count)""",
    },
    "hub-genes": {
        "help": "Genes participating in the most distinct pathways, graph-wide. --limit (default 10).",
        "query": """\
SELECT ?symbol (COUNT(DISTINCT ?pathway) AS ?pathway_count) WHERE {{
  ?gene a biolink:Gene ; rdfs:label ?symbol .
  ?assoc biolink:subject ?gene ; biolink:object ?pathway .
}} GROUP BY ?symbol ORDER BY DESC(?pathway_count) LIMIT {limit}""",
    },
    "shared-pathways": {
        "help": "Pathways two genes both participate in. Requires --genes A,B.",
        "needs_gene_pair": True,
        "query": """\
SELECT ?label WHERE {{
  ?geneA a biolink:Gene ; rdfs:label "{gene_a}" .
  ?geneB a biolink:Gene ; rdfs:label "{gene_b}" .
  ?assocA biolink:subject ?geneA ; biolink:object ?pathway .
  ?assocB biolink:subject ?geneB ; biolink:object ?pathway .
  ?pathway rdfs:label ?label .
}} ORDER BY ?label""",
    },
    "evidence-breakdown": {
        "help": "Curated (TAS) vs orthology-inferred (IEA) association counts for a gene. Requires --gene.",
        "needs_gene": True,
        "query": """\
SELECT ?evidence (COUNT(*) AS ?n) WHERE {{
  ?gene a biolink:Gene ; rdfs:label "{gene}" .
  ?assoc biolink:subject ?gene ; biolink:has_evidence ?evidence .
}} GROUP BY ?evidence ORDER BY DESC(?n)""",
    },
    "go-terms-for-gene": {
        "help": "GO Biological Process terms reachable via a gene's pathways (gene -> pathway -> GO crosswalk). Requires --gene.",
        "needs_gene": True,
        "query": """\
SELECT DISTINCT ?go WHERE {{
  ?gene a biolink:Gene ; rdfs:label "{gene}" .
  ?assoc biolink:subject ?gene ; biolink:object ?pathway .
  ?pathway skos:closeMatch ?go .
}} ORDER BY ?go""",
    },
}

RESULT_TYPES = {
    "tsv": "text/tab-separated-values",
    "csv": "text/csv",
    "json": "application/sparql-results+json",
}


def build_canned_query(name: str, args: argparse.Namespace) -> str:
    spec = CANNED_QUERIES[name]
    if spec.get("needs_gene") and not args.gene:
        raise ValueError(f"--canned {name} requires --gene SYMBOL")
    if spec.get("needs_gene_pair"):
        if not args.genes or len(args.genes) != 2:
            raise ValueError(f"--canned {name} requires --genes A,B (exactly two symbols)")
    if spec.get("needs_panel"):
        panel = args.panel or "als"
        if panel not in DISEASE_GENE_PANELS:
            raise ValueError(f"--panel must be one of {sorted(DISEASE_GENE_PANELS)}, got {panel!r}")

    if name == "disease-gene-panel":
        panel = args.panel or "als"
        symbols = " ".join(f'"{s}"' for s in DISEASE_GENE_PANELS[panel])
        return spec["query"].format(symbols=symbols)
    if name == "shared-pathways":
        gene_a, gene_b = args.genes
        return spec["query"].format(gene_a=gene_a, gene_b=gene_b)
    if name == "hub-genes":
        return spec["query"].format(limit=args.limit)
    if spec.get("needs_gene"):
        return spec["query"].format(gene=args.gene)
    return spec["query"]


def build_query(body: str) -> str:
    prefix_lines = "\n".join(f"PREFIX {name}: <{uri}>" for name, uri in sorted(PREFIXES.items()))
    return f"{prefix_lines}\n{body}"


def parse_genes(value: str) -> list[str]:
    symbols = [s.strip() for s in value.split(",") if s.strip()]
    for symbol in symbols:
        _symbol(symbol, "--genes")
    return symbols


def run_query(endpoint: str, query: str, fmt: str, timeout: float,
              graph: str | None = None) -> str:
    params = {"query": query}
    if graph:
        params["default-graph-uri"] = graph
    data = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(
        endpoint, data=data, headers={"Accept": RESULT_TYPES[fmt]}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def query_store(path: Path, query: str, fmt: str, graph: str | None = None) -> str:
    import pyoxigraph

    if not path.is_dir():
        raise IngestError(f"No Oxigraph store at {path}. Run reactome.pipeline first.")
    store = pyoxigraph.Store.read_only(str(path))
    options = {"default_graph": pyoxigraph.NamedNode(graph)} if graph else {}
    result = store.query(query, **options)
    return result.serialize(format=pyoxigraph.QueryResultsFormat.from_media_type(
        RESULT_TYPES[fmt]
    )).decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", nargs="?", help="Raw SPARQL query; reads stdin if omitted and --canned is not given")
    parser.add_argument(
        "--canned", choices=sorted(CANNED_QUERIES), metavar="NAME",
        help="Run a canned demo query instead of a custom one. Choices: "
        + ", ".join(f"{n} ({s['help']})" for n, s in CANNED_QUERIES.items()),
    )
    parser.add_argument("--gene", type=lambda v: _symbol(v, "--gene"), help="HGNC symbol, e.g. APP or SOD1")
    parser.add_argument("--genes", type=parse_genes, help="Comma-separated symbols, e.g. APP,PSEN1")
    parser.add_argument("--panel", choices=sorted(DISEASE_GENE_PANELS), help="Disease gene panel (default: als)")
    parser.add_argument("--limit", type=int, default=10, help="Row limit for hub-genes (default: 10)")
    parser.add_argument("--version", default="97", help="Release graph for canned queries (default: 97)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--endpoint", help="Oxigraph query URL, e.g. http://localhost:7010/query")
    target.add_argument("--store", type=Path, help="Default: reactome/v<version>/data/store")
    parser.add_argument("--format", choices=sorted(RESULT_TYPES), default="tsv")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.query and args.canned:
        parser.error("pass a raw query or --canned, not both")
    if args.limit < 1:
        parser.error("--limit must be positive")

    if args.canned:
        try:
            body = build_canned_query(args.canned, args)
        except ValueError as error:
            parser.error(str(error))
    else:
        body = args.query if args.query is not None else sys.stdin.read()
        if not body.strip():
            parser.error("no SPARQL query provided (pass one, or use --canned)")

    full_query = build_query(body)
    endpoint = args.endpoint or (DEFAULT_ENDPOINT if args.store is None else None)
    try:
        graph = graph_uri(args.version) if args.canned else None
        if endpoint:
            result = run_query(endpoint, full_query, args.format, args.timeout, graph)
        else:
            path = args.store or Path(__file__).resolve().parent / f"v{args.version.lstrip('vV')}/data/store"
            result = query_store(path, full_query, args.format, graph)
    except urllib.error.HTTPError as error:
        sys.stderr.write(f"error: {endpoint} returned {error.code}:\n{error.read().decode(errors='replace')}\n")
        return 1
    except urllib.error.URLError as error:
        sys.stderr.write(
            f"error: could not reach {endpoint}: {error}\n"
            "Start Oxigraph with: docker compose -f reactome/compose.yaml up -d\n"
        )
        return 1
    except (IngestError, OSError, SyntaxError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1

    sys.stdout.write(result)
    if not result.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
