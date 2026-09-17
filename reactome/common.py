"""Species filters, evidence codes, identifiers and TSV helpers for Reactome."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

# Re-export RDF helpers with the Reactome prefix map below.
from shared.rdf import (  # noqa: F401
    NAMESPACES,
    IngestError,
    iri,
    literal,
    log,
    open_maybe_gzip,
    typed_literal,
)
from shared.rdf import TurtleWriter as _TurtleWriter
from shared.rdf import expand as _expand_with_prefixes


# ── species ───────────────────────────────────────────────────────────────────

HUMAN = "Homo sapiens"
HUMAN_TAXON_CURIE = "NCBITaxon:9606"

# Every species Reactome ships, so an unrecognised name is a hard error rather
# than a silent drop.  A silent drop looks like sparse data, not like a bug
# (plan section 8).  Verified against ReactomePathways.txt for release V97.
SPECIES_NAME_TO_TAXON_ID = {
    "Homo sapiens": "9606",
    "Mus musculus": "10090",
    "Rattus norvegicus": "10116",
    "Bos taurus": "9913",
    "Gallus gallus": "9031",
    "Sus scrofa": "9823",
    "Canis familiaris": "9615",
    "Danio rerio": "7955",
    "Xenopus tropicalis": "8364",
    "Drosophila melanogaster": "7227",
    "Caenorhabditis elegans": "6239",
    "Dictyostelium discoideum": "44689",
    "Schizosaccharomyces pombe": "4896",
    "Saccharomyces cerevisiae": "4932",
    "Plasmodium falciparum": "5833",
    "Mycobacterium tuberculosis": "1773",
}


class UnknownSpeciesError(IngestError):
    pass


class UnmappedEvidenceCodeError(IngestError):
    pass


def taxon_curie_for_species(species_name: str) -> str:
    """Map a Reactome species *name* to an NCBITaxon CURIE.

    Species is a name string in every Reactome flat file, never a taxon ID
    (plan section 8).  Unrecognised names raise rather than being dropped.
    """
    taxon_id = SPECIES_NAME_TO_TAXON_ID.get(species_name)
    if taxon_id is None:
        raise UnknownSpeciesError(
            f"Unrecognised Reactome species name {species_name!r}. "
            "Add it to SPECIES_NAME_TO_TAXON_ID rather than dropping the row."
        )
    return f"NCBITaxon:{taxon_id}"


@dataclass
class SpeciesFilter:
    """The single shared species filter (plan section 9).

    Two modes, because the three core files are not shaped alike:

    * ``keep_row``           -- files that carry a species-name column
                               (ReactomePathways.txt col 3,
                                UniProt2Reactome_All_Levels.txt col 6)
    * ``keep_by_membership`` -- ReactomePathwaysRelation.txt, which has no
                               species column and must be filtered against the
                               already-filtered pathway ID set.

    Filtering is on the species *name*, never on the ``R-HSA-`` infix of the
    stable ID (plan section 4).  String-sniffing the stable ID happens to work
    today and would fail silently, by omission, the moment scope widens.
    """

    species_name: str = HUMAN
    kept: int = 0
    dropped: int = 0
    dropped_by_species: dict[str, int] = field(default_factory=dict)

    @property
    def taxon_curie(self) -> str:
        return taxon_curie_for_species(self.species_name)

    def keep_row(self, row: Sequence[str], species_column: int) -> bool:
        """Species-name filter for a file that has a species column.

        ``species_column`` is 1-based to match the column tables in the plan.
        """
        name = row[species_column - 1].strip()
        # Validate every name we see, including ones we are about to drop:
        # that is what turns a renamed species into an error instead of a
        # quiet shortfall in the output.
        taxon_curie_for_species(name)
        if name == self.species_name:
            self.kept += 1
            return True
        self.dropped += 1
        self.dropped_by_species[name] = self.dropped_by_species.get(name, 0) + 1
        return False

    def keep_by_membership(self, ids: Iterable[str], allowed_ids: set[str]) -> bool:
        """Membership filter for files with no species column of their own.

        Every referenced ID must be in the in-scope set; a row referencing a
        mix would otherwise hang a non-human subtree off a human parent
        (plan section 8).
        """
        if all(i in allowed_ids for i in ids):
            self.kept += 1
            return True
        self.dropped += 1
        return False

    def summary(self) -> str:
        top = sorted(self.dropped_by_species.items(), key=lambda kv: -kv[1])[:5]
        detail = ", ".join(f"{n}={c}" for n, c in top)
        return (
            f"species filter [{self.species_name}]: kept={self.kept:,} "
            f"dropped={self.dropped:,}" + (f" (top dropped: {detail})" if detail else "")
        )


# ── evidence codes ────────────────────────────────────────────────────────────

# Plan section 4.  Fail loudly on any unmapped code rather than defaulting.
EVIDENCE_CODE_TO_ECO = {
    "TAS": "ECO:0000304",  # traceable author statement -- manually curated
    "IEA": "ECO:0000501",  # inferred from electronic annotation -- orthology-projected
}


def eco_curie_for_evidence(code: str) -> str:
    eco = EVIDENCE_CODE_TO_ECO.get(code.strip())
    if eco is None:
        raise UnmappedEvidenceCodeError(
            f"Unmapped Reactome evidence code {code!r}. "
            f"Known codes: {sorted(EVIDENCE_CODE_TO_ECO)}. "
            "Extend EVIDENCE_CODE_TO_ECO -- do not default."
        )
    return eco


# ── URIs ──────────────────────────────────────────────────────────────────────

# One namespace for model terms. Anything this ingest needs that the model has
# not ratified is still minted under sagebrain: and reported as undefined by
# shared/model_terms.py, rather than hidden in a second namespace.
PREFIXES = {
    **NAMESPACES,
    "UNIPROT": "https://identifiers.org/uniprot:",
    "GO": "http://purl.obolibrary.org/obo/GO_",
    "ECO": "http://purl.obolibrary.org/obo/ECO_",
    "pav": "http://purl.org/pav/",
}

REACTOME_SOURCE = "infores:reactome"


def expand(curie: str) -> str:
    """Expand a CURIE to a full IRI using this ingest's PREFIXES."""
    return _expand_with_prefixes(curie, PREFIXES)


def pathway_curie(stable_id: str) -> str:
    return f"REACT:{stable_id}"


def uniprot_curie(accession: str) -> str:
    return f"UNIPROT:{accession}"


def hgnc_curie(hgnc_id: str) -> str:
    """HGNC IDs arrive as ``HGNC:7765``; identifiers.org keys on the number."""
    return f"HGNC:{hgnc_id.split(':', 1)[-1]}"


def go_curie(go_id: str) -> str:
    """GO IDs arrive as ``GO:0000165``."""
    return f"GO:{go_id.split(':', 1)[-1]}"


def base_accession(accession: str) -> str:
    """Strip a UniProt isoform suffix (``P21359-2`` -> ``P21359``).

    Used for identifier resolution only.  The full accession, isoform suffix
    included, is what gets carried on the association (plan section 5.3).
    """
    return accession.split("-", 1)[0]


# ── TSV reading ───────────────────────────────────────────────────────────────

# ReactomePathwaysRelation.txt is the only file where a leading token could be
# mistaken for a header; the others are checked structurally.  These are the
# header rows observed at V97 -- see verify_columns.py, which is the real check.
KNOWN_HEADER_FIRST_FIELDS = {"identifier", "stable_id", "uniprot", "source"}


def read_tsv(
    path: Path,
    expected_fields: int,
    *,
    has_header: bool,
    filename_must_contain: str | None = None,
) -> Iterator[list[str]]:
    """Stream a Reactome TSV, enforcing shape.

    * ``expected_fields`` -- every row must have exactly this many columns.
    * ``has_header``      -- the plan says these files have no header rows, but
      ``Pathways2GoTerms_human.txt`` does.  Pass the verified value; a header
      must never be ingested silently as data (plan section 3).
    * ``filename_must_contain`` -- assert e.g. ``_All_Levels``.  Reactome ships
      both propagated and leaf-only variants and pulling the wrong one
      under-reports with no error (plan section 8).
    """
    path = Path(path)
    if filename_must_contain and filename_must_contain not in path.name:
        raise IngestError(
            f"Expected a filename containing {filename_must_contain!r}, got {path.name!r}. "
            "Reactome ships both propagated and leaf-only variants; the wrong one "
            "under-reports silently."
        )

    with open_maybe_gzip(path) as handle:
        reader = csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE)
        for lineno, row in enumerate(reader, start=1):
            if not row or (len(row) == 1 and not row[0].strip()):
                continue
            if lineno == 1:
                looks_like_header = row[0].strip().lower() in KNOWN_HEADER_FIRST_FIELDS
                if has_header:
                    if not looks_like_header:
                        raise IngestError(
                            f"{path.name}: expected a header row but first field is "
                            f"{row[0]!r}. Re-run verify_columns.py."
                        )
                    continue
                if looks_like_header:
                    raise IngestError(
                        f"{path.name}: unexpected header row {row!r}. The layout changed "
                        "-- re-run verify_columns.py before ingesting."
                    )
            if len(row) != expected_fields:
                raise IngestError(
                    f"{path.name}:{lineno}: expected {expected_fields} columns, "
                    f"got {len(row)}: {row!r}"
                )
            yield row

# ── Turtle emission ───────────────────────────────────────────────────────────

class TurtleWriter(_TurtleWriter):
    """Turtle writer with Reactome prefixes."""

    def __init__(self, handle, prefixes: dict[str, str] | None = None):
        super().__init__(handle, prefixes or PREFIXES)
