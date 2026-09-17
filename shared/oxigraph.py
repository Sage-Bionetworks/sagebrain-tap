"""Oxigraph graph loading and SPARQL access shared by ingests."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

from .rdf import IngestError, log


def load_oxigraph(store_path: Path, graph: str, parts: list[Path], void_path: Path) -> None:
    try:
        import pyoxigraph
    except ImportError as error:  # pragma: no cover
        raise IngestError(
            "pyoxigraph is required for --store. pip install pyoxigraph, or use "
            "--endpoint to load into an external triple store."
        ) from error

    store_path.mkdir(parents=True, exist_ok=True)
    store = pyoxigraph.Store(str(store_path))
    target = pyoxigraph.NamedNode(graph)

    # Drop-and-reload: the whole point of a per-release graph.
    store.remove_graph(target)

    for part in parts:
        log(f"Loading {part.name} ...")
        with open(part, "rb") as handle:
            store.bulk_load(handle, format=pyoxigraph.RdfFormat.TURTLE, to_graph=target)

    log(f"Loading {void_path.name} into the default graph ...")
    store.update(f"DELETE WHERE {{ <{graph}> ?p ?o }}")
    with open(void_path, "rb") as handle:
        store.bulk_load(handle, format=pyoxigraph.RdfFormat.TURTLE)

    store.optimize()
    loaded = sum(1 for _ in store.quads_for_pattern(None, None, None, target))
    log(f"\n<{graph}> now holds {loaded:,} triples in {store_path}")


def load_endpoint(endpoint: str, graph: str, parts: list[Path], void_path: Path) -> None:
    """Replace a release and its metadata using Oxigraph's /store and /update."""
    endpoint = endpoint.rstrip("/")
    if not endpoint.endswith("/store"):
        raise IngestError("Expected an Oxigraph Graph Store URL ending in /store.")
    update_endpoint = endpoint.removesuffix("/store") + "/update"
    graph_url = f"{endpoint}?graph={urllib.parse.quote(graph, safe='')}"
    uploads = [("PUT" if index == 0 else "POST", graph_url, part)
               for index, part in enumerate(parts)]
    uploads.append(("POST", f"{endpoint}?default", void_path))
    for method, url, part in uploads:
        if part == void_path:
            request = urllib.request.Request(
                update_endpoint, data=f"DELETE WHERE {{ <{graph}> ?p ?o }}".encode(),
                headers={"Content-Type": "application/sparql-update"},
            )
            with urllib.request.urlopen(request, timeout=300):
                pass
        log(f"{method} {part.name} -> {url}")
        request = urllib.request.Request(
            url,
            data=part.read_bytes(),
            method=method,
            headers={"Content-Type": "text/turtle; charset=utf-8"},
        )
        with urllib.request.urlopen(request, timeout=1800) as response:
            if response.status >= 300:
                raise IngestError(f"{method} {url} returned {response.status}")


class GraphClient:
    """Read SPARQL results from a local store or an HTTP query endpoint."""

    def __init__(self, store: Path | None = None, endpoint: str | None = None,
                 graphs: list[str] | None = None):
        self.endpoint = endpoint
        self.graphs = graphs or []
        self.store = None
        if endpoint is None:
            import pyoxigraph
            if store is None or not store.is_dir():
                raise IngestError(f"No Oxigraph store at {store}. Run the pipeline first.")
            self.store = pyoxigraph.Store.read_only(str(store))

    def select(self, query: str) -> dict:
        if self.store is not None:
            import pyoxigraph
            nodes = [pyoxigraph.NamedNode(g) for g in self.graphs]
            options = {"default_graph": nodes, "named_graphs": nodes} if nodes else {}
            result = self.store.query(query, **options)
            return json.loads(result.serialize(format=pyoxigraph.QueryResultsFormat.JSON))
        params = [("query", query)]
        for graph in self.graphs:
            params.extend([("default-graph-uri", graph), ("named-graph-uri", graph)])
        request = urllib.request.Request(
            self.endpoint, data=urllib.parse.urlencode(params).encode(),
            headers={"Accept": "application/sparql-results+json"},
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.load(response)
