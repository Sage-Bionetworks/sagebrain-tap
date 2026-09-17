"""Summarise an ingested Reactome release's data characteristics as JSON.

Read from the emitted Turtle, not from the source TSVs and not from a loaded
store.  The Turtle is what the graph asserts and what downstream consumers parse
back (synthetic/common.py reads these same files for its gene sets), so a
characteristic measured anywhere else could be true of the inputs and false of
the release.

The structural caveats are the point of the file.  _All_Levels associations are
pre-propagated, pathway gene sets are therefore nested and often duplicated, and
per-pathway gene counts are not independent categories.  Those are reported as
measurements -- containment per hierarchy edge, identical gene sets, the
size band a scorer would keep -- rather than left as a warning in a comment."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from shared.summary import distribution, num, repo_relative

from .common import (
    EVIDENCE_CODE_TO_ECO,
    IngestError,
    log,
)

#: Gene-set size band a rank-based scorer keeps: below the floor a score is
#: dominated by single genes, above the ceiling a Reactome set is a whole
#: top-level domain.  Mirrors synthetic/common.py rather than importing it --
#: the dependency runs the other way, since that module scores THIS output.
DEFAULT_MIN_SET_SIZE = 15
DEFAULT_MAX_SET_SIZE = 500

#: Enough rows to see whether the biggest sets are one domain or many.
TOP_N = 20

CORE_FILES = ("pathways.ttl", "associations.ttl", "go_crosswalk.ttl")


def local_name(term: str) -> str:
    """``biolink:original_subject`` -> ``original_subject``.

    Matched on the local name because the term carrying the source accession has
    changed more than once, and so has its prefix. A summary that silently
    reported zero accessions for a release written under an older name would be
    worse than one that refused to parse it.
    """
    return term.rsplit(":", 1)[-1]


def iter_blocks(path: Path):
    """Yield ``(subject, [(predicate, object), ...])`` per Turtle block.

    A line-oriented read of our own deterministic writer's output -- the same
    assumption shared/rdf.py's TurtleWriter fixes and synthetic/common.py
    already relies on -- so this stays stdlib-only and streams a 50 MB
    associations file without holding it in memory.
    """
    subject: str | None = None
    pairs: list[tuple[str, str]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "@prefix")):
                continue
            if not line[0].isspace():
                if subject is not None:
                    yield subject, pairs
                subject, pairs = stripped, []
                continue
            predicate, _, obj = stripped.rstrip(" ;.").partition(" ")
            pairs.append((predicate, obj.strip()))
    if subject is not None:
        yield subject, pairs


def unquote(value: str) -> str:
    """Turtle literal -> its lexical form, datatype and quotes removed."""
    if value.startswith('"""') and value.endswith('"""'):
        return value[3:-3]
    if value.startswith('"'):
        return value[1:value.rindex('"')].replace('\\"', '"').replace("\\n", "\n")
    return value


def statement_count(path: Path) -> int:
    """Predicate-object pairs in a file, which is its triple count."""
    return sum(len(pairs) for _subject, pairs in iter_blocks(path))


# ── the three core files ──────────────────────────────────────────────────────


class Release:
    """Everything one pass over each Turtle file can answer."""

    def __init__(self) -> None:
        self.labels: dict[str, str] = {}
        self.types: Counter = Counter()
        self.parents: dict[str, set[str]] = defaultdict(set)
        self.taxa: Counter = Counter()
        self.gene_symbols: dict[str, str] = {}
        self.sets: dict[str, set[str]] = defaultdict(set)
        self.gene_pathways: dict[str, set[str]] = defaultdict(set)
        self.associations = 0
        self.evidence: Counter = Counter()
        self.knowledge_sources: Counter = Counter()
        self.accessions: set[str] = set()
        self.isoform_accessions: set[str] = set()
        self.go_terms: dict[str, set[str]] = defaultdict(set)


def read_pathways(path: Path, release: Release) -> None:
    for subject, pairs in iter_blocks(path):
        if not subject.startswith("REACT:"):
            continue
        for predicate, obj in pairs:
            if predicate == "a":
                release.types[obj] += 1
            elif predicate == "rdfs:label":
                release.labels[subject] = unquote(obj)
            elif predicate == "biolink:part_of":
                # The writer emits one pair per line; a comma list is still
                # legal Turtle, so both shapes are accepted.
                for token in obj.split(","):
                    token = token.strip()
                    if token.startswith("REACT:"):
                        release.parents[subject].add(token)
            elif predicate == "biolink:in_taxon":
                release.taxa[obj] += 1


def read_associations(path: Path, release: Release) -> None:
    for subject, pairs in iter_blocks(path):
        fields = {local_name(predicate): obj for predicate, obj in pairs}
        if subject.startswith("HGNC:"):
            # Genes are typed nodes, not bare association endpoints: a consumer
            # can find every gene in the release without reading 161k
            # associations, and an untyped endpoint would show up here as a
            # count that disagrees with the association subjects.
            release.types[fields.get("a", "untyped")] += 1
            release.gene_symbols[subject] = unquote(fields.get("label", ""))
            # Genes carry in_taxon too, so the tally covers every node in the
            # release rather than just the pathways.
            if "in_taxon" in fields:
                release.taxa[fields["in_taxon"]] += 1
            continue
        if fields.get("a") != "biolink:GeneToPathwayAssociation":
            continue
        gene, pathway = fields.get("subject"), fields.get("object")
        if not gene or not pathway:
            raise IngestError(
                f"{path.name}: an association block is missing a subject or object "
                f"({subject} {pairs}). The file is not this ingest's output."
            )
        release.associations += 1
        release.types[fields["a"]] += 1
        release.sets[pathway].add(gene)
        release.gene_pathways[gene].add(pathway)
        release.evidence[fields.get("has_evidence", "none")] += 1
        release.knowledge_sources[fields.get("primary_knowledge_source", "none")] += 1
        # biolink:original_subject today; sagebrain:uniprot_accession and, before
        # the namespaces merged, sbkg:source_accession. Releases on disk outlive
        # a rename, and quietly reporting zero accessions for an older one would
        # be worse than reading all three names.
        accession = unquote(fields.get("original_subject")
                            or fields.get("uniprot_accession")
                            or fields.get("source_accession") or "")
        if accession:
            release.accessions.add(accession)
            if "-" in accession.rsplit(":", 1)[-1]:
                release.isoform_accessions.add(accession)


def read_go_crosswalk(path: Path, release: Release) -> None:
    for subject, pairs in iter_blocks(path):
        if not subject.startswith("REACT:"):
            continue
        for predicate, obj in pairs:
            if predicate == "skos:closeMatch":
                release.go_terms[subject].update(
                    token.strip() for token in obj.split(",") if token.strip()
                )


def read_void(path: Path) -> dict | None:
    """Release-level provenance, as asserted in the default graph.

    Reported rather than recomputed: void:triples is the loader's own count, and
    a summary that disagreed with it would mean the graph and its description
    had drifted apart -- which is worth seeing, not papering over.
    """
    if not path.exists():
        return None
    wanted = {
        "rdfs:label": "label",
        "dcterms:source": "source",
        "pav:version": "version",
        "dcterms:issued": "issued",
        "dcterms:license": "license",
        "void:triples": "triples",
        "biolink:in_taxon": "in_taxon",
        "prov:wasGeneratedBy": "generated_by",
    }
    out: dict = {}
    for subject, pairs in iter_blocks(path):
        if not subject.startswith("<urn:sagebrain:reactome:"):
            continue
        out["dataset"] = subject.strip("<>")
        for predicate, obj in pairs:
            key = wanted.get(predicate)
            if not key:
                continue
            value = unquote(obj.split("^^")[0]).strip("<>")
            out[key] = int(value) if key == "triples" else value
    return out or None


def read_sources(path: Path) -> dict | None:
    """The committed source manifest: what was downloaded, and its checksum."""
    if not path.exists():
        log(f"  {path} not found; source manifest omitted")
        return None
    header: dict[str, str] = {}
    files: list[dict] = []
    columns: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            # "# published: 2026-06  license: cc-by-4.0" carries two entries
            # on one line; the qualifier in "provenance (authoritative)" is a
            # note to a reader, not part of the key.
            for part in line.lstrip("# ").split("  "):
                key, sep, value = part.partition(":")
                if sep and value.strip():
                    key = key.split("(")[0].strip().lower().replace(" ", "_")
                    header[key] = value.strip()
            continue
        fields = line.split("\t")
        if not columns:
            columns = fields
            continue
        row = dict(zip(columns, fields))
        if "bytes" in row and row["bytes"].isdecimal():
            row["bytes"] = int(row["bytes"])
        files.append(row)
    return {"header": header, "files": files}


# ── derived views ─────────────────────────────────────────────────────────────


def hierarchy_section(release: Release) -> dict:
    """Shape of the part_of DAG: roots, leaves, multi-parent nodes, depth.

    A DAG, not a tree.  Reactome attaches a pathway under every parent it
    genuinely belongs to, so "how many pathways" and "how many places a pathway
    appears" are different numbers, and depth is the LONGEST path to a root.
    """
    pathways = set(release.labels)
    children: dict[str, set[str]] = defaultdict(set)
    edges = 0
    for child, parents in release.parents.items():
        for parent in parents:
            children[parent].add(child)
            edges += 1

    depth_of: dict[str, int] = {}
    on_stack: set[str] = set()
    cycles = 0

    def depth(pathway: str) -> int:
        nonlocal cycles
        if pathway in depth_of:
            return depth_of[pathway]
        if pathway in on_stack:
            # Acceptance check 5 asserts the hierarchy is acyclic; if that ever
            # stops holding, say so instead of recursing forever.
            cycles += 1
            return 0
        on_stack.add(pathway)
        parents = release.parents.get(pathway, ())
        value = 1 + max((depth(p) for p in parents), default=-1)
        on_stack.discard(pathway)
        depth_of[pathway] = value
        return value

    for pathway in sorted(pathways):
        depth(pathway)

    roots = sorted(p for p in pathways if not release.parents.get(p))
    return {
        "part_of_edges": edges,
        "root_pathways": len(roots),
        "leaf_pathways": sum(1 for p in pathways if p not in children),
        "pathways_with_multiple_parents": sum(
            1 for p in pathways if len(release.parents.get(p, ())) > 1),
        "cycles_detected": cycles,
        "parents_per_pathway": distribution(
            [len(release.parents.get(p, ())) for p in sorted(pathways)]),
        "children_per_pathway": distribution(
            [len(children.get(p, ())) for p in sorted(pathways)]),
        "depth_below_root": distribution([depth_of[p] for p in sorted(pathways)]),
        "roots": [{"pathway": p, "label": release.labels.get(p, p),
                   "direct_children": len(children.get(p, ()))} for p in roots],
    }


def propagation_section(release: Release) -> dict:
    """What _All_Levels propagation did to the gene sets.

    The single most consequential fact about this ingest for anyone scoring
    pathways: a parent's gene set is a superset of its children's, so a child
    signal necessarily drags its ancestors along and a list of "significant
    pathways" is a subtree, not a count of findings.  Measured per hierarchy
    edge as |child genes in parent| / |child genes|.
    """
    containments: list[float] = []
    fully_contained = 0
    for child in sorted(release.parents):
        child_genes = release.sets.get(child)
        if not child_genes:
            continue
        for parent in sorted(release.parents[child]):
            parent_genes = release.sets.get(parent)
            if not parent_genes:
                continue
            fraction = len(child_genes & parent_genes) / len(child_genes)
            containments.append(fraction)
            fully_contained += fraction == 1.0

    # Pathways no gene-set method can tell apart, however different their
    # labels.  Cheap to find by hashing the sets, and it bounds the resolution
    # of every downstream enrichment result.
    by_set: dict[frozenset, list[str]] = defaultdict(list)
    for pathway, genes in release.sets.items():
        by_set[frozenset(genes)].append(pathway)
    duplicate_groups = sorted(
        (sorted(members) for members in by_set.values() if len(members) > 1),
        key=lambda members: (-len(members), members[0]),
    )

    return {
        "note": ("UniProt2Reactome_All_Levels.txt is already transitively closed "
                 "up the hierarchy; per-pathway gene counts are not independent "
                 "categories and must not be propagated again."),
        "child_parent_pairs_compared": len(containments),
        "child_genes_in_parent": distribution(containments, digits=4),
        "fully_contained_children": fully_contained,
        "fully_contained_fraction": num(
            fully_contained / max(len(containments), 1), 4),
        "identical_gene_set_groups": len(duplicate_groups),
        "pathways_in_identical_groups": sum(len(g) for g in duplicate_groups),
        "largest_identical_group": [
            {"pathway": p, "label": release.labels.get(p, p)}
            for p in (duplicate_groups[0] if duplicate_groups else [])
        ],
    }


def gene_set_section(release: Release, min_size: int, max_size: int) -> dict:
    sizes = {pathway: len(genes) for pathway, genes in release.sets.items()}
    in_band = [p for p, size in sizes.items() if min_size <= size <= max_size]
    without_genes = sorted(set(release.labels) - set(release.sets))

    top_pathways = sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]
    top_genes = sorted(
        ((gene, len(pathways)) for gene, pathways in release.gene_pathways.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )[:TOP_N]

    return {
        "genes_per_pathway": distribution(
            [sizes.get(p, 0) for p in sorted(release.labels)]),
        "pathways_per_gene": distribution(
            [len(release.gene_pathways[g]) for g in sorted(release.gene_pathways)]),
        "pathways_without_genes": len(without_genes),
        "scoring_band": {
            "min_set_size": min_size,
            "max_set_size": max_size,
            "pathways_in_band": len(in_band),
            "below_band": sum(1 for size in sizes.values() if size < min_size),
            "above_band": sum(1 for size in sizes.values() if size > max_size),
            "note": ("Sizes are over the full release. A scorer intersects these "
                     "sets with its own expressed-gene universe first, so its "
                     "in-band count will be lower."),
        },
        "largest_pathways": [
            {"pathway": p, "label": release.labels.get(p, p), "genes": size}
            for p, size in top_pathways
        ],
        "hub_genes": [
            {"gene": g, "symbol": release.gene_symbols.get(g, ""), "pathways": n}
            for g, n in top_genes
        ],
    }


def describe(ttl_dir: Path, version: str, sources_path: Path) -> dict:
    ttl_dir = Path(ttl_dir)
    missing = [name for name in CORE_FILES if not (ttl_dir / name).exists()]
    if missing:
        raise SystemExit(
            f"{', '.join(missing)} not found in {ttl_dir}. These are the "
            f"characteristics of a real ingest, so there is nothing to describe:\n"
            f"  python -m reactome.pipeline --version {version}"
        )

    release = Release()
    log("Reading pathways ...")
    read_pathways(ttl_dir / "pathways.ttl", release)
    log(f"  {len(release.labels):,} pathway nodes")
    log("Reading associations (the large file) ...")
    read_associations(ttl_dir / "associations.ttl", release)
    log(f"  {release.associations:,} associations over {len(release.gene_symbols):,} genes")
    log("Reading GO crosswalk ...")
    read_go_crosswalk(ttl_dir / "go_crosswalk.ttl", release)

    eco_to_code = {eco: code for code, eco in EVIDENCE_CODE_TO_ECO.items()}
    unmapped_evidence = sorted(set(release.evidence) - set(eco_to_code))

    void = read_void(ttl_dir / "void.ttl")
    files = {
        name: {
            "bytes": (ttl_dir / name).stat().st_size,
            "statements": statement_count(ttl_dir / name),
        }
        for name in (*CORE_FILES, "void.ttl")
        if (ttl_dir / name).exists()
    }
    # The loader puts exactly the three core files in the release graph and
    # VoID's own statements in the default graph, so these two numbers must
    # agree. When they do not, the graph and its self-description have drifted.
    core_statements = sum(files[name]["statements"] for name in CORE_FILES if name in files)

    return {
        "release": {
            "version": version,
            "graph": f"urn:sagebrain:reactome:v{version}",
            "ttl_dir": repo_relative(ttl_dir),
            "types_asserted": dict(release.types.most_common()),
            "taxa_asserted": dict(release.taxa.most_common()),
            "statements_in_release_graph": core_statements,
            "matches_void_triples": (
                None if not void or "triples" not in void
                else core_statements == void["triples"]
            ),
            "void": void,
        },
        "sources": read_sources(sources_path),
        "files": files,
        "pathways": {
            "count": len(release.labels),
            "with_gene_set": len(release.sets),
            "with_go_term": len(release.go_terms),
            "hierarchy": hierarchy_section(release),
        },
        "genes": {
            "count": len(release.gene_symbols),
            "with_symbol": sum(1 for s in release.gene_symbols.values() if s),
            "in_an_association": len(release.gene_pathways),
        },
        "associations": {
            "count": release.associations,
            "evidence": {
                eco_to_code.get(eco, eco): count
                for eco, count in release.evidence.most_common()
            },
            "unmapped_evidence_codes": unmapped_evidence,
            "knowledge_sources": dict(release.knowledge_sources.most_common()),
            "source_accessions": len(release.accessions),
            "isoform_accessions": len(release.isoform_accessions),
        },
        "gene_sets": gene_set_section(release, DEFAULT_MIN_SET_SIZE, DEFAULT_MAX_SET_SIZE),
        "propagation": propagation_section(release),
        "go_crosswalk": {
            "pathways_with_a_term": len(release.go_terms),
            "edges": sum(len(terms) for terms in release.go_terms.values()),
            "terms_per_pathway": distribution(
                [len(terms) for terms in release.go_terms.values()]),
            "note": ("skos:closeMatch, not owl:equivalentClass: Reactome pathways "
                     "and GO BP terms are not co-extensive."),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", default="97", help="Reactome release, e.g. 97")
    parser.add_argument("--ttl-dir", type=Path, default=None,
                        help="Default: reactome/v<version>/data/rdf")
    parser.add_argument("--sources", type=Path, default=None,
                        help="Source manifest to embed. "
                             "Default: reactome/manifests/v<version>-sources.tsv")
    parser.add_argument("--out", type=Path, default=None,
                        help="Default: reactome/manifests/v<version>-characteristics.json, "
                             "or - for stdout")
    args = parser.parse_args()

    version = args.version.lstrip("vV")
    if not version.isdecimal():
        parser.error("--version must be a release number, e.g. 97")

    ttl_dir = args.ttl_dir or Path(f"reactome/v{version}/data/rdf")
    sources = args.sources or Path(f"reactome/manifests/v{version}-sources.tsv")
    out = args.out or Path(f"reactome/manifests/v{version}-characteristics.json")

    payload = describe(ttl_dir, version, sources)
    text = json.dumps(payload, indent=2) + "\n"

    if str(out) == "-":
        sys.stdout.write(text)
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    hierarchy = payload["pathways"]["hierarchy"]
    propagation = payload["propagation"]
    log(f"  {payload['pathways']['count']:,} pathways, "
        f"{payload['genes']['count']:,} genes, "
        f"{payload['associations']['count']:,} associations")
    log(f"  {hierarchy['part_of_edges']:,} part_of edges, "
        f"{hierarchy['root_pathways']} roots, "
        f"max depth {hierarchy['depth_below_root']['max']}")
    log(f"  {propagation['fully_contained_fraction']:.1%} of child gene sets sit "
        f"entirely inside a parent; {propagation['identical_gene_set_groups']} "
        f"groups of pathways share an identical gene set")
    log(f"\nWrote {out} ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
