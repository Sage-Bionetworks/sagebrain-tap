"""Backend regression tests. Set TEST_OXIGRAPH_URL to include HTTP checks."""

import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pyoxigraph

from reactome import pipeline
from reactome.common import IngestError
from reactome.load_graph import TTL_PARTS, build_void, graph_uri, load_endpoint, load_oxigraph
from reactome.queries import (
    CANNED_QUERIES, build_canned_query, build_query, query_store, run_query,
)


class OxigraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "store"
        self.graph = graph_uri("99997")
        self.other_graph = graph_uri("99996")
        self.endpoint = os.environ.get("TEST_OXIGRAPH_URL")
        self.parts = [self.root / name for name in TTL_PARTS]
        bodies = [
            'REACT:R-HSA-1 a biolink:Pathway; rdfs:label "Pathway one" .',
            '''HGNC:1 a biolink:Gene; rdfs:label "SOD1" .
               [] a biolink:GeneToPathwayAssociation;
                  biolink:subject HGNC:1; biolink:object REACT:R-HSA-1;
                  biolink:has_evidence ECO:0000304 .''',
            'REACT:R-HSA-1 skos:closeMatch GO:0000165 .',
        ]
        # SPARQL PREFIX declarations are also valid Turtle directives.
        for path, body in zip(self.parts, bodies):
            path.write_text(build_query(body))
        self.void = self.root / "void.ttl"
        self.void.write_text(f'<{self.graph}> <http://rdfs.org/ns/void#triples> 9 .')
        self.load(self.graph)

    def load(self, graph):
        load_oxigraph(self.store, graph, self.parts, self.void)
        if self.endpoint:
            load_endpoint(f"{self.endpoint}/store", graph, self.parts, self.void)

    def query(self, body, fmt="json", graph=None):
        query = build_query(body)
        local = query_store(self.store, query, fmt, graph)
        if self.endpoint:
            remote = run_query(f"{self.endpoint}/query", query, fmt, 30, graph)
            if fmt == "json":
                self.assertEqual(json.loads(local), json.loads(remote))
            else:
                self.assertEqual(local, remote)
        return local

    def test_panel_keeps_missing_genes_and_selects_one_release(self):
        self.load(self.other_graph)
        args = argparse.Namespace(panel="als", gene=None, genes=None, limit=10)
        body = build_canned_query("disease-gene-panel", args)
        rows = json.loads(self.query(body, graph=self.graph))["results"]["bindings"]
        counts = {row["symbol"]["value"]: int(row["pathway_count"]["value"]) for row in rows}
        self.assertEqual(counts, {"SOD1": 1, "FUS": 0, "C9orf72": 0, "TARDBP": 0})
        # A different membership in the other release must not leak into queries.
        self.parts[1].write_text(build_query('''HGNC:1 a biolink:Gene; rdfs:label "SOD1" .
            [] biolink:subject HGNC:1; biolink:object REACT:R-HSA-2 .'''))
        self.load(self.other_graph)
        rows = json.loads(self.query(body, graph=self.graph))["results"]["bindings"]
        self.assertEqual(int(rows[0]["pathway_count"]["value"]), 1)

    def test_reload_replaces_only_target_release(self):
        self.load(self.other_graph)
        self.parts[1].write_text("")
        self.load(self.graph)
        count = 'SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }'
        current = json.loads(self.query(count, graph=self.graph))["results"]["bindings"]
        previous = json.loads(self.query(count, graph=self.other_graph))["results"]["bindings"]
        self.assertEqual(int(current[0]["n"]["value"]), 3)
        self.assertEqual(int(previous[0]["n"]["value"]), 9)
        metadata = self.query(f'SELECT ?n WHERE {{ <{self.graph}> <http://rdfs.org/ns/void#triples> ?n }}')
        self.assertEqual(len(json.loads(metadata)["results"]["bindings"]), 1)

    def test_all_canned_queries_parse_and_execute(self):
        args = argparse.Namespace(gene="SOD1", genes=["SOD1", "FUS"], panel="als", limit=10)
        for name in CANNED_QUERIES:
            with self.subTest(name=name):
                result = self.query(build_canned_query(name, args), graph=self.graph)
                self.assertIn("results", json.loads(result))

    def test_standard_result_formats(self):
        body = 'SELECT ?symbol WHERE { ?gene a biolink:Gene; rdfs:label ?symbol }'
        self.assertEqual(self.query(body, "tsv", self.graph).strip(), '?symbol\n"SOD1"')
        self.assertEqual(self.query(body, "csv", self.graph).splitlines(), ["symbol", "SOD1"])

    def test_raw_query_selects_its_own_graph(self):
        result = self.query(f'SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{self.graph}> {{ ?s ?p ?o }} }}')
        self.assertEqual(json.loads(result)["results"]["bindings"][0]["n"]["value"], "9")

    def test_invalid_version_and_missing_store(self):
        with self.assertRaises(IngestError):
            graph_uri("97> }")
        with self.assertRaises(IngestError):
            query_store(self.root / "missing", "SELECT * WHERE {}", "json")

    def test_void_records_doi_count_and_date_in_default_graph(self):
        build_void("99997", {
            "doi": "10.5281/zenodo.21383214", "version": 'V99997 "test"',
            "publication_date": "2026-06", "license_id": "cc-by-4.0",
        }, 9, "2026-09-15", self.void)
        self.load(self.graph)
        rows = json.loads(self.query(f'''SELECT ?doi ?n ?issued WHERE {{
            <{self.graph}> dcterms:source ?doi; void:triples ?n; dcterms:issued ?issued
        }}'''))["results"]["bindings"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["doi"]["value"], "https://doi.org/10.5281/zenodo.21383214")
        self.assertEqual(rows[0]["n"]["value"], "9")
        self.assertEqual(rows[0]["issued"]["value"], "2026-06-01")

    def test_reload_replaces_metadata_for_only_its_release(self):
        self.void.write_text(f'<{self.other_graph}> <http://rdfs.org/ns/void#triples> 9 .')
        self.load(self.other_graph)
        self.void.write_text(f'<{self.graph}> <http://rdfs.org/ns/void#triples> 3 .')
        self.load(self.graph)
        body = f'''SELECT ?g ?n WHERE {{
            VALUES ?g {{ <{self.graph}> <{self.other_graph}> }}
            ?g <http://rdfs.org/ns/void#triples> ?n
        }} ORDER BY ?g'''
        rows = json.loads(self.query(body))["results"]["bindings"]
        self.assertEqual({r["g"]["value"]: r["n"]["value"] for r in rows},
                         {self.graph: "3", self.other_graph: "9"})
        self.assertEqual(len(rows), 2)


class PipelineTests(unittest.TestCase):
    def test_remote_pipeline_uses_distinct_load_and_query_urls(self):
        argv = ["pipeline", "--version", "97", "--skip-download", "--round-trip", "0",
                "--input-dir", "inputs", "--workdir", "outputs",
                "--endpoint", "http://localhost:7010/"]
        with patch("sys.argv", argv), patch.object(pipeline, "run") as run:
            pipeline.main()
        calls = {call.args[0]: call.args[1] for call in run.call_args_list}
        load, checks = calls["load_graph"], calls["acceptance_checks"]
        self.assertEqual(load[load.index("--endpoint") + 1], "http://localhost:7010/store")
        self.assertEqual(checks[checks.index("--endpoint") + 1], "http://localhost:7010/query")
        self.assertEqual(load[load.index("--ttl-dir") + 1], str(Path("outputs/rdf").resolve()))
        self.assertNotIn("download_sources", calls)


if __name__ == "__main__":
    unittest.main()
