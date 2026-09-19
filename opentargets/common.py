"""Identifiers, controlled vocabularies and Parquet helpers for Open Targets.

Open Targets differs from Reactome in three ways that shape this module.

1. **The release is a directory of Parquet, not flat TSVs.** Some datasets are
   Spark output (``part-00000-<uuid>-c000.snappy.parquet`` plus a ``_SUCCESS``
   marker), others a single ``<name>.parquet``. The part names carry a
   per-release UUID, so the file list is discovered, never hardcoded.
2. **Upstream publishes its own integrity manifest.** Every file in the release
   is listed with a SHA-1 in ``release_data_integrity``, and that file has its
   own ``.sha1``. So downloads are verified against the *publisher's* digests
   rather than ours alone, and the digest of the integrity manifest anchors the
   release the way Reactome's Zenodo version DOI does.
3. **Targets are Ensembl gene ids.** Reactome resolves UniProt to HGNC so genes
   are one kind of node across the graph; this ingest resolves Ensembl to HGNC
   for the same reason, and keeps the Ensembl id on the association. Measured at
   26.06: 1,548 of 1,550 mechanism targets resolve (99.9%).

Controlled vocabularies below are recorded as complete sets, observed at 26.06,
and membership is enforced. That is the Reactome species/evidence discipline: a
value the ingest has never seen is an error, because the alternative is dropping
it silently, and a silent drop looks like sparse data rather than a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from shared.rdf import (  # noqa: F401
    NAMESPACES,
    IngestError,
    iri,
    literal,
    log,
    typed_literal,
)
from shared.rdf import TurtleWriter as _TurtleWriter
from shared.rdf import expand as _expand_with_prefixes


# ── release layout ────────────────────────────────────────────────────────────

DEFAULT_RELEASE = "26.06"

FTP_BASE = "https://ftp.ebi.ac.uk/pub/databases/opentargets/platform/{release}"

#: Upstream's per-file SHA-1 listing, and its own digest. The pair is this
#: ingest's provenance anchor: pinning the manifest's digest pins the manifest,
#: and the manifest pins every byte of the release.
INTEGRITY_FILE = "release_data_integrity"
INTEGRITY_SHA1_FILE = "release_data_integrity.sha1"

#: Datasets are under `output/` within a release.
OUTPUT_DIR = "output"


@dataclass(frozen=True)
class Dataset:
    """One Open Targets dataset directory.

    ``required_columns`` is the layout gate (``verify_schemas.py``). It lists the
    columns this ingest reads, not every column the dataset has: a new upstream
    column is not a problem, a vanished one is. Open Targets reorganises datasets
    between releases -- ``clinical_indication`` and ``clinical_report`` are a
    recent split out of a combined ``indication`` dataset -- so the gate is what
    turns that reorganisation into an error at the start of a run instead of a
    puzzling absence at the end.
    """

    name: str
    required_columns: frozenset[str]
    note: str
    #: False for datasets pinned and gated but not yet projected to RDF.
    projected: bool = True


DATASETS: dict[str, Dataset] = {
    "drug_molecule": Dataset(
        name="drug_molecule",
        required_columns=frozenset({
            "id", "name", "drugType", "inchiKey", "canonicalSmiles",
            "synonyms", "tradeNames", "parentId", "maximumClinicalStage",
        }),
        note="ChEMBL molecules: preferred name, synonyms, trade names, structure keys",
    ),
    "drug_mechanism_of_action": Dataset(
        name="drug_mechanism_of_action",
        required_columns=frozenset({
            "actionType", "mechanismOfAction", "chemblIds",
            "targetName", "targetType", "targets",
        }),
        note="Mechanism rows: action type and Ensembl gene targets, keyed to a LIST of drugs",
    ),
    "clinical_indication": Dataset(
        name="clinical_indication",
        required_columns=frozenset({
            "id", "drugId", "diseaseId", "maxClinicalStage", "clinicalReportIds",
        }),
        note="Drug-disease pairs with the maximum clinical stage reached",
    ),
    "clinical_report": Dataset(
        name="clinical_report",
        required_columns=frozenset({
            "id", "type", "source", "clinicalStage", "drugs", "diseases",
            "trialWhyStopped", "trialStopReasonCategories", "hasExpertReview",
            "trialStartDate", "url",
        }),
        note="Evidence behind each indication: trials, labels, regulatory records. "
             "Pinned and gated; not projected in the first pass -- indications carry "
             "a report COUNT, and the reports themselves are a layer of their own",
        projected=False,
    ),
    "disease": Dataset(
        name="disease",
        required_columns=frozenset({
            "id", "name", "dbXRefs", "ancestors", "therapeuticAreas", "exactSynonyms",
        }),
        note="Disease/phenotype terms: labels and synonyms for the indication axis",
    ),
}


# ── controlled vocabularies (complete at 26.06; membership enforced) ──────────

#: Every clinical-stage value any pinned dataset uses at 26.06:
#: ``clinical_indication.maxClinicalStage``, ``drug_molecule.maximumClinicalStage``
#: and ``clinical_report.clinicalStage``. Membership is enforced.
#:
#: ``clinical_report`` is the one that widens this set -- it alone uses ``PHASE_4``
#: (30,509 rows) and ``WITHDRAWAL`` (866). Both are listed here even though no
#: projected dataset uses them, because the vocabulary gate now audits that column
#: too and a set that covered only the projected datasets would pass today and fail
#: the moment reports are projected.
CLINICAL_STAGES = frozenset({
    "UNKNOWN", "PRECLINICAL", "IND", "EARLY_PHASE_1", "PHASE_1", "PHASE_1_2",
    "PHASE_2", "PHASE_2_3", "PHASE_3", "PHASE_4", "PREAPPROVAL", "APPROVAL",
    "WITHDRAWAL",
})

#: The subset that lies on the development axis, ordered weakest to strongest, so
#: "highest stage reached" is expressible without a consumer hardcoding the order.
#:
#: ``UNKNOWN`` sorts first: it means no phase was recorded, not an early one.
#: ``PHASE_4`` sorts above ``APPROVAL`` because phase-4 studies are post-marketing.
#:
#: ``WITHDRAWAL`` is deliberately ABSENT. It is a terminal outcome, not a rung: a
#: withdrawn drug reached approval and was then pulled, so ranking it above
#: APPROVAL would read as further progress and ranking it at the bottom would read
#: as never developed. Both are wrong, so it is not ranked at all and `stage_rank`
#: refuses it rather than choosing a wrong answer quietly.
CLINICAL_STAGE_ORDER = (
    "UNKNOWN",
    "PRECLINICAL",
    "IND",
    "EARLY_PHASE_1",
    "PHASE_1",
    "PHASE_1_2",
    "PHASE_2",
    "PHASE_2_3",
    "PHASE_3",
    "PREAPPROVAL",
    "APPROVAL",
    "PHASE_4",
)
CLINICAL_STAGE_RANK = {stage: rank for rank, stage in enumerate(CLINICAL_STAGE_ORDER)}

#: ChEMBL mechanism action types, all 30 observed at 26.06. Emitted as a literal
#: rather than mapped onto Biolink predicates: "OPENER" and "STABILISER" have no
#: faithful Biolink predicate, and inventing a mapping would assert a direction
#: of effect the source does not state.
ACTION_TYPES = frozenset({
    "ACTIVATOR", "AGONIST", "ALLOSTERIC ANTAGONIST", "ANTAGONIST",
    "ANTISENSE INHIBITOR", "BINDING AGENT", "BLOCKER", "CROSS-LINKING AGENT",
    "DEGRADER", "DISRUPTING AGENT", "EXOGENOUS GENE", "EXOGENOUS PROTEIN",
    "GENE EDITING NEGATIVE MODULATOR", "HYDROLYTIC ENZYME", "INHIBITOR",
    "INVERSE AGONIST", "MODULATOR", "NEGATIVE ALLOSTERIC MODULATOR",
    "NEGATIVE MODULATOR", "OPENER", "OTHER", "PARTIAL AGONIST",
    "POSITIVE ALLOSTERIC MODULATOR", "POSITIVE MODULATOR", "PROTEOLYTIC ENZYME",
    "RELEASING AGENT", "RNAI INHIBITOR", "STABILISER", "SUBSTRATE",
    "VACCINE ANTIGEN",
})

#: ChEMBL target types, all 9 observed at 26.06.
#:
#: The distinction matters and is kept on every mechanism edge. For a
#: ``single protein`` row, ``targets`` is one gene and the edge means what it
#: looks like. For ``protein family`` / ``protein complex`` / ``selectivity
#: group``, ``targets`` enumerates the MEMBERS -- trametinib's "MEK1/2" row
#: lists MAP2K1 and MAP2K2 -- so those edges are membership of a named group,
#: not a measured interaction with each gene. A query that wants strict
#: drug-to-target pairs filters on SINGLE_TARGET_TYPES.
TARGET_TYPES = frozenset({
    "chimeric protein", "nucleic-acid", "protein complex", "protein complex group",
    "protein family", "protein nucleic-acid complex", "protein-protein interaction",
    "selectivity group", "single protein",
})
SINGLE_TARGET_TYPES = frozenset({"single protein", "chimeric protein", "nucleic-acid"})

#: ``drugType`` values, all 11 observed at 26.06.
DRUG_TYPES = frozenset({
    "Antibody", "Antibody drug conjugate", "Cell", "Enzyme", "Gene",
    "Oligonucleotide", "Oligosaccharide", "Protein", "Small molecule",
    "Unknown", "Vaccine component",
})


#: Biolink class per ``drugType``.
#:
#: A deliberately shallow split. "Small molecule" is 18,124 of 22,407 molecules and
#: has an exact Biolink class; the other ten modalities span antibodies, proteins,
#: oligonucleotides, gene and cell therapies, and mapping each onto a Biolink class
#: would assert distinctions this ingest cannot check. They take the common
#: supertype and keep ``sagebrain:drug_type`` verbatim, so a consumer can refine on
#: the source's own word rather than on our guess. ``biolink:ChemicalEntity`` is
#: admittedly a loose fit for the 67 "Cell" entries; the drugType says so.
DRUG_TYPE_CLASS = {"Small molecule": "biolink:SmallMolecule"}
DRUG_TYPE_CLASS_DEFAULT = "biolink:ChemicalEntity"

#: Biolink class per disease-id prefix.
#:
#: ``clinical_indication`` mixes diseases with phenotypes: 8,591 of its rows point
#: at HP terms and 430 at MP (mouse phenotype) terms. Typing those as
#: ``biolink:Disease`` would be wrong, so the prefix decides. GO and OBA terms are
#: neither; they take the union class rather than being forced either way.
DISEASE_NODE_CLASS = {
    "MONDO": "biolink:Disease",
    "EFO": "biolink:Disease",
    "Orphanet": "biolink:Disease",
    "NCIT": "biolink:Disease",
    "DOID": "biolink:Disease",
    "OTAR": "biolink:Disease",
    "HP": "biolink:PhenotypicFeature",
    "MP": "biolink:PhenotypicFeature",
}
DISEASE_NODE_CLASS_DEFAULT = "biolink:DiseaseOrPhenotypicFeature"


def drug_type_class(drug_type: str) -> str:
    check_vocabulary(drug_type, DRUG_TYPES, "drugType", "DRUG_TYPES")
    return DRUG_TYPE_CLASS.get(drug_type, DRUG_TYPE_CLASS_DEFAULT)


def disease_node_class(disease_id: str) -> str:
    prefix = disease_id.partition("_")[0]
    return DISEASE_NODE_CLASS.get(prefix, DISEASE_NODE_CLASS_DEFAULT)


def check_vocabulary(value: str, allowed: frozenset[str], what: str, constant: str) -> str:
    """Enforce a controlled vocabulary, naming the constant to extend.

    Reactome's rule, for the same reason: extend the set deliberately, never
    default or drop. The message names the constant so the fix does not require
    reading this module.
    """
    if value not in allowed:
        raise IngestError(
            f"Unrecognised {what} {value!r}. Extend {constant} in opentargets/common.py "
            f"after checking what the release means by it -- do not default or drop."
        )
    return value


def stage_rank(stage: str) -> int:
    """Position on the development axis. Raises for stages that have none.

    Separate from membership on purpose: `CLINICAL_STAGES` says a value is valid,
    this says where it sits, and WITHDRAWAL is the case where those two differ.
    A caller computing "highest stage reached" over a set containing WITHDRAWAL
    has to decide what that means; silently ranking it would decide for them.
    """
    if stage in CLINICAL_STAGE_RANK:
        return CLINICAL_STAGE_RANK[stage]
    if stage in CLINICAL_STAGES:
        raise IngestError(
            f"Clinical stage {stage!r} is valid but is not on the development axis, "
            f"so it cannot be ranked. Decide explicitly how to treat it -- see "
            f"CLINICAL_STAGE_ORDER in opentargets/common.py."
        )
    raise IngestError(
        f"Unrecognised clinical stage {stage!r}. Extend CLINICAL_STAGES in "
        "opentargets/common.py, and CLINICAL_STAGE_ORDER too if it is a "
        "development stage rather than a terminal outcome."
    )


# ── identifiers ───────────────────────────────────────────────────────────────

PREFIXES = {
    **NAMESPACES,
    "CHEMBL": "https://identifiers.org/chembl:",
    "ENSEMBL": "https://identifiers.org/ensembl:",
    "EFO": "http://www.ebi.ac.uk/efo/",
    "obo": "http://purl.obolibrary.org/obo/",
    "ORDO": "http://www.orpha.net/ORDO/",
    "pav": "http://purl.org/pav/",
}

OPENTARGETS_SOURCE = "infores:open-targets"

#: How each disease-id prefix Open Targets uses becomes an IRI.
#:
#: The zoo is real: ``clinical_indication`` at 26.06 keys on MONDO (70,521 rows),
#: HP (8,591), EFO (6,300), Orphanet (432), MP (430), GO (154), OBA (35), NCIT (3)
#: and OTAR (2). OBO-library terms take the OBO purl; EFO-native and OTAR terms
#: take the EFO base, which is where both are published; Orphanet takes ORDO.
#:
#: Unknown prefixes raise. A disease id silently turned into a wrong IRI is worse
#: than a failed run: it would join to nothing and look like absent data.
DISEASE_IRI_BASES = {
    "EFO": "EFO",
    "OTAR": "EFO",
    "Orphanet": "ORDO",
    "MONDO": "obo",
    "HP": "obo",
    "MP": "obo",
    "GO": "obo",
    "OBA": "obo",
    "NCIT": "obo",
    "DOID": "obo",
    "OBI": "obo",
    "PATO": "obo",
    "OGMS": "obo",
    "UBERON": "obo",
    "GSSO": "obo",
}


def chembl_curie(chembl_id: str) -> str:
    """``CHEMBL2103875`` -> ``CHEMBL:CHEMBL2103875``.

    identifiers.org keys ChEMBL on the full accession, prefix included, so the
    local part keeps its ``CHEMBL`` -- unlike HGNC, which keys on the number.
    """
    value = chembl_id.strip()
    if not value.startswith("CHEMBL"):
        raise IngestError(f"Not a ChEMBL identifier: {chembl_id!r}")
    return f"CHEMBL:{value}"


def ensembl_curie(gene_id: str) -> str:
    return f"ENSEMBL:{gene_id.strip()}"


def hgnc_curie(hgnc_id: str) -> str:
    """HGNC ids arrive as ``HGNC:7765``; identifiers.org keys on the number."""
    return f"HGNC:{hgnc_id.split(':', 1)[-1]}"


def disease_curie(disease_id: str) -> str:
    """An Open Targets disease id -> a CURIE in this ingest's prefix map."""
    value = disease_id.strip()
    prefix, sep, _ = value.partition("_")
    if not sep:
        raise IngestError(
            f"Disease id {disease_id!r} has no ``<PREFIX>_<local>`` shape; "
            "the disease key scheme changed."
        )
    base = DISEASE_IRI_BASES.get(prefix)
    if base is None:
        raise IngestError(
            f"No IRI base registered for disease id prefix {prefix!r} (from {disease_id!r}). "
            "Add it to DISEASE_IRI_BASES in opentargets/common.py -- guessing an IRI "
            "would produce a node that joins to nothing and reads as absent data."
        )
    return f"{base}:{value}"


def expand(curie: str) -> str:
    return _expand_with_prefixes(curie, PREFIXES)


def release_graph(release: str) -> str:
    """The named graph for a release, e.g. ``urn:sagebrain:opentargets:26.06``."""
    return f"urn:sagebrain:opentargets:{release}"


# ── Parquet reading ───────────────────────────────────────────────────────────


def dataset_dir(input_dir: Path, name: str) -> Path:
    path = Path(input_dir) / name
    if not path.is_dir():
        raise IngestError(
            f"No {name!r} directory under {input_dir}. "
            f"Run: python -m opentargets.download_sources --release <release>"
        )
    return path


def read_dataset(input_dir: Path, name: str, columns: list[str] | None = None):
    """Open one dataset directory as a single pyarrow dataset, columns gated.

    pyarrow reads a directory of parts as one table, which is why the multi-part
    layout needs no special handling downstream. Requested columns are checked
    against the schema here so a renamed column fails by name rather than as a
    ``KeyError`` several frames deeper.
    """
    import pyarrow.dataset as pads

    spec = DATASETS.get(name)
    if spec is None:
        raise IngestError(f"Unknown Open Targets dataset {name!r}")
    data = pads.dataset(dataset_dir(input_dir, name))
    present = set(data.schema.names)
    missing = sorted(spec.required_columns - present)
    if missing:
        raise IngestError(
            f"{name}: required column(s) {missing} are absent. Open Targets "
            f"reorganises datasets between releases -- re-check the layout before "
            f"ingesting (python -m opentargets.verify_schemas)."
        )
    if columns:
        unknown = sorted(set(columns) - present)
        if unknown:
            raise IngestError(f"{name}: no such column(s) {unknown}")
    return data


def iter_rows(input_dir: Path, name: str, columns: list[str],
              batch_size: int = 50_000) -> Iterator[dict]:
    """Stream a dataset as dicts, one batch at a time.

    Batched rather than ``to_table().to_pylist()``: clinical_report is 71 MB of
    Parquet with nested trial records and expands by more than an order of
    magnitude in Python objects.
    """
    data = read_dataset(input_dir, name, columns)
    for batch in data.to_batches(columns=columns, batch_size=batch_size):
        yield from batch.to_pylist()


# ── Ensembl -> HGNC ───────────────────────────────────────────────────────────


@dataclass
class HgncResolver:
    """Ensembl gene id -> HGNC id, from the HGNC complete set.

    Genes are HGNC-keyed across this repo (Reactome resolves UniProt the same
    way), so an Ensembl-keyed source is resolved on the way in rather than
    leaving consumers to join two kinds of gene node. The Ensembl id is kept on
    the association as ``biolink:original_object``, mirroring how Reactome keeps
    the UniProt accession it came from.

    An Ensembl id mapping to more than one HGNC id fans out, as Reactome's
    multiple-match rule does. Three such ids exist in the release used at 26.06.
    """

    ensembl_to_hgnc: dict[str, list[str]]
    symbols: dict[str, str]
    resolved: int = 0
    unresolved: int = 0
    unresolved_ids: set[str] | None = None

    @classmethod
    def from_file(cls, path: Path) -> "HgncResolver":
        import csv

        path = Path(path)
        if not path.exists():
            raise IngestError(
                f"HGNC complete set not found at {path}. It is the Ensembl -> HGNC "
                "crosswalk; fetch it with reactome.download_sources or pass --hgnc."
            )
        mapping: dict[str, list[str]] = {}
        symbols: dict[str, str] = {}
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for column in ("hgnc_id", "symbol", "ensembl_gene_id"):
                if column not in (reader.fieldnames or []):
                    raise IngestError(
                        f"{path.name} has no {column!r} column; the HGNC layout changed."
                    )
            for row in reader:
                hgnc_id = (row.get("hgnc_id") or "").strip()
                ensembl = (row.get("ensembl_gene_id") or "").strip()
                if hgnc_id:
                    symbols[hgnc_id] = (row.get("symbol") or "").strip()
                if hgnc_id and ensembl:
                    mapping.setdefault(ensembl, []).append(hgnc_id)
        if not mapping:
            raise IngestError(f"{path.name} yielded no Ensembl -> HGNC pairs.")
        return cls(ensembl_to_hgnc=mapping, symbols=symbols, unresolved_ids=set())

    def resolve(self, ensembl_id: str) -> list[str]:
        hits = self.ensembl_to_hgnc.get(ensembl_id.strip(), [])
        if hits:
            self.resolved += 1
        else:
            self.unresolved += 1
            if self.unresolved_ids is not None:
                self.unresolved_ids.add(ensembl_id)
        return hits

    def symbol(self, hgnc_id: str) -> str:
        return self.symbols.get(hgnc_id, "")

    @property
    def unresolved_fraction(self) -> float:
        total = self.resolved + self.unresolved
        return self.unresolved / total if total else 0.0

    def summary(self) -> str:
        total = self.resolved + self.unresolved
        return (f"Ensembl -> HGNC: resolved {self.resolved:,} of {total:,} lookups "
                f"({100 * (1 - self.unresolved_fraction):.1f}%)")


# ── Turtle emission ───────────────────────────────────────────────────────────


class TurtleWriter(_TurtleWriter):
    """Turtle writer with the Open Targets prefix map."""

    def __init__(self, handle, prefixes: dict[str, str] | None = None):
        super().__init__(handle, prefixes or PREFIXES)
