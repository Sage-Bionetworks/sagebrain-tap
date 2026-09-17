"""Shared Turtle emission, literal escaping and namespace bases."""

from __future__ import annotations

import gzip
import sys
from pathlib import Path
from typing import Sequence


class IngestError(RuntimeError):
    """Raised for any condition an ingest's plan says to fail loudly on."""


# ── literals and IRIs ─────────────────────────────────────────────────────────

_ESCAPES = str.maketrans(
    {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
)


def literal(value: str) -> str:
    return '"' + value.translate(_ESCAPES) + '"'


def typed_literal(value: str, datatype: str) -> str:
    return f"{literal(value)}^^{datatype}"


def iri(url: str) -> str:
    return f"<{url}>"


# One model namespace. Terms an ingest needs before the model has ratified them
# are still minted here, and reported by shared/model_terms.py as undefined --
# a second "ingest extensions" namespace only made them easy to forget.
NAMESPACES = {
    "sagebrain": "https://w3id.org/synapse/sagebrain#",
    "biolink": "https://w3id.org/biolink/vocab/",
    "infores": "https://w3id.org/biolink/infores/",
    "REACT": "https://identifiers.org/reactome:",
    "HGNC": "https://identifiers.org/hgnc:",
    "NCBITaxon": "http://purl.obolibrary.org/obo/NCBITaxon_",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "dcterms": "http://purl.org/dc/terms/",
    "prov": "http://www.w3.org/ns/prov#",
    "void": "http://rdfs.org/ns/void#",
}


def expand(curie: str, prefixes: dict[str, str]) -> str:
    """Expand a CURIE to a full IRI against a caller-supplied prefix map.

    The prefix map is a parameter rather than a module constant on purpose: it
    is the one piece of this file that is not generic.
    """
    prefix, _, local = curie.partition(":")
    base = prefixes.get(prefix)
    if base is None:
        raise IngestError(f"No prefix registered for CURIE {curie!r}")
    return base + local


def open_maybe_gzip(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, "rt", encoding="utf-8", newline="")


# ── Turtle emission ───────────────────────────────────────────────────────────


class TurtleWriter:
    """Minimal streaming Turtle writer.

    Streaming rather than building an rdflib graph: the Reactome association
    transform emits on the order of a million triples and the synthetic score
    transform emits comparably many, and there is no reason to hold either in
    memory.  Output is deterministic, so releases diff cleanly.
    """

    def __init__(self, handle, prefixes: dict[str, str]):
        self.handle = handle
        self.triples = 0
        for prefix, base in sorted(prefixes.items()):
            handle.write(f"@prefix {prefix}: <{base}> .\n")
        handle.write("\n")

    def comment(self, text: str) -> None:
        for line in text.splitlines():
            self.handle.write(f"# {line}\n")
        self.handle.write("\n")

    def statements(self, subject: str, pairs: Sequence[tuple[str, str]]) -> None:
        """Write one subject block: ``s p o ; p o .``"""
        if not pairs:
            return
        self.handle.write(subject + "\n")
        for index, (predicate, obj) in enumerate(pairs):
            terminator = " ." if index == len(pairs) - 1 else " ;"
            self.handle.write(f"    {predicate} {obj}{terminator}\n")
        self.handle.write("\n")
        self.triples += len(pairs)

    def blank_node(self, pairs: Sequence[tuple[str, str]]) -> None:
        """Write a reified association as a blank node."""
        self.handle.write("[]\n")
        for index, (predicate, obj) in enumerate(pairs):
            terminator = " ." if index == len(pairs) - 1 else " ;"
            self.handle.write(f"    {predicate} {obj}{terminator}\n")
        self.handle.write("\n")
        self.triples += len(pairs)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
