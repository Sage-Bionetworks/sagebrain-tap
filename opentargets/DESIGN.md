# Open Targets design decisions

This document records the drug-layer model and validation contract. See
[README.md](README.md) for operations and release statistics,
[TRIALS.md](TRIALS.md) for the scoped but unbuilt trial layer,
[datasets/](datasets/) for per-dataset source notes, and
[manifests/](manifests/) for recorded results.

## 1. Release contents and ingest scope

Open Targets Platform 26.06 is a collection of related datasets. Its
`release_data_integrity` inventory lists 56 directories under
`output/`, covering targets, diseases, drugs, clinical records, genetic evidence,
association scores and supporting annotations. This ingest downloads five of
those datasets as Parquet; each can contain one file or several parts.

The selected inputs are listed below. Counts are **source rows in release 26.06**,
from the [source manifest](manifests/26.06-sources.tsv), not emitted edge counts.
The [column report](manifests/26.06-column-verification.md) records their layouts.

| Source dataset | Rows | What the source contains | What this ingest emits |
|---|---:|---|---|
| [`drug_molecule`](datasets/drug_molecule.md) | 22,407 | ChEMBL IDs, names, synonyms, trade names, modality, structures, parent molecule and overall clinical stage | Molecule nodes and properties; a separate label TSV |
| [`drug_mechanism_of_action`](datasets/drug_mechanism_of_action.md) | 6,500 | Lists of ChEMBL drugs and Ensembl targets, action, mechanism text and target type/name | Drug–gene associations and HGNC gene nodes; rows expand across drugs and resolved targets |
| [`clinical_indication`](datasets/clinical_indication.md) | 86,468 | Drug–disease pairs, maximum clinical stage and supporting report IDs | Indication associations with stage and report count |
| [`disease`](datasets/disease.md) | 47,080 | MONDO, EFO and other disease/phenotype IDs, names, synonyms, ontology ancestry, cross-references and therapeutic areas | Typed nodes, labels and synonyms for the 3,749 terms referenced by indications; no ontology hierarchy |
| [`clinical_report`](datasets/clinical_report.md) | 289,955 | Trials, labels and regulatory records, including stage, drugs, diseases, trial dates, stop reasons and review information | Nothing yet: downloaded, pinned and validated; the trial layer scoped in [TRIALS.md](TRIALS.md) is not built |

Open Targets uses an [EFO-based disease/phenotype ontology](https://platform-docs.opentargets.org/disease-or-phenotype)
that includes MONDO terms. In 26.06, 70,521 of 86,468 indication rows (about 82%)
reference MONDO IDs; others use HP (HPO), EFO, Orphanet, MP and additional prefixes
listed in the [prefix audit](manifests/26.06-column-verification.md#disease-id-prefixes-used-by-clinical_indication).
The ingest preserves these source identifiers rather than normalizing all terms
to MONDO.

ChEMBL IDs connect molecules to mechanisms and indications; disease IDs connect
indications to term labels. Mechanism targets are Ensembl IDs, resolved through a
separately pinned HGNC snapshot so they share gene identity with the Reactome
ingest and join its gene nodes directly by IRI. The indication transform counts
`clinicalReportIds` directly: it neither joins to `clinical_report` nor emits the
report IDs or individual report records.

The remaining 51 output directories are currently not downloaded or projected by the
default pipeline, though the pipeline may additionally tap these later. Examples from the pinned release inventory:

| Excluded data | Dataset examples | Scope decision |
|---|---|---|
| Target–disease scores | `association_overall_*`, `association_by_datasource_*`, `association_by_datatype_*` | Outside the initial drug-layer scope; may be added later for ranking, alongside the evidence they summarize |
| Supporting evidence and genetics | `evidence_*`, `variant`, `study`, `credible_set`, `colocalisation`, `l2g_prediction` | Separate evidence and variant models would be needed |
| Target and biological annotations | `target`, `target_essentiality`, `target_prioritisation`, `baseline_expression`, `interaction`, `reactome`, `go` | This pass emits only the gene nodes needed by mechanisms |
| Derived drug–target–disease join | `clinical_target` | Reproducible from datasets already ingested, and with a coarser clinical stage |
| Additional clinical and drug data | `drug_warning`, `pharmacogenomics`, `openfda_significant_adverse_drug_reactions` | Outside the current molecule/mechanism/indication model |

`clinical_target` is excluded on different grounds than the rest since it is somewhat
redundant with what the release already builds from `clinical_report` and
`drug_mechanism_of_action` under the same report QC filter as `clinical_indication`;
its drug–target–disease triples are exactly the join of those two datasets on
drug. Its `maxClinicalStage` is scoped to the drug–target pair, so reaching the same
claim through the drug keeps the more precise per-pair `sagebrain:max_clinical_stage`.
The trials it adds, whose diseases never mapped to an ontology term, are already in
the pinned `clinical_report`.

Selecting a dataset also does not imply usage of all its fields. For example,
molecule `crossReferences` and disease ancestry/cross-references stay out of the
graph. [common.py](common.py)'s `DATASETS` defines the downloaded subset and
required columns; the transforms define which values are emitted.

### Ingest flow

Solid arrows show data flow. Dashed branches show retained or excluded data with
no RDF output. HGNC is an independent input, outside the Open Targets release.

```mermaid
flowchart TD
    release["Open Targets 26.06 release<br/>56 output directories"]
    selected["Download five selected datasets<br/>Verify files against integrity manifest"]
    gate["Validate required columns,<br/>vocabularies and indication ID prefixes"]
    omitted["51 directories excluded<br/>Scores, evidence, genetics, target annotations,<br/>derived joins, additional clinical/drug data and other outputs"]

    release --> selected --> gate
    release -.-> omitted

    molecules["drug_molecule<br/>Transform molecules"]
    mechanisms["drug_mechanism_of_action<br/>Resolve targets and transform mechanisms"]
    indications["clinical_indication + disease<br/>Transform indications and referenced terms"]
    reports["clinical_report<br/>Pinned and validated; trial layer scoped, not built"]
    hgnc["HGNC snapshot<br/>Verify separate release pin"]
    labels["drug_molecule<br/>Export label TSV outside RDF"]

    gate --> molecules
    gate --> mechanisms
    gate --> indications
    gate --> labels
    gate -.-> reports
    hgnc --> mechanisms

    load["Load into Oxigraph<br/>Release graph + default-graph VoID metadata"]
    molecules -->|molecules.ttl| load
    mechanisms -->|mechanisms.ttl| load
    indications -->|indications.ttl| load
    load --> checks["Run acceptance checks<br/>on the loaded graph"]

    classDef deferred fill:#fff4ce,stroke:#8a6500,color:#333;
    classDef excluded fill:#f0f0f0,stroke:#666,color:#333;
    class reports deferred;
    class omitted excluded;
```

The diagram shows dependencies; [pipeline.py](pipeline.py) runs the transforms
and label export sequentially. The three transforms read the verified source
files independently, then the loader combines their Turtle outputs.

## 2. Sources and provenance

Discover dataset filenames from FTP: Spark part names can change each release.
Verify and record the inputs in this order:

1. Verify `release_data_integrity` against `release_data_integrity.sha1`.
2. Verify each downloaded part against that manifest.
3. Commit `manifests/<release>-sources.tsv` with upstream SHA-1, local SHA-256,
   byte counts and row counts. Record the manifest's SHA-1 as the release anchor
   in `release.json` and VoID metadata.

Every pinned part must be present and unchanged; extra, unpinned Parquet files
also fail verification because transforms read whole dataset directories.
`--skip-download` still verifies local inputs.

HGNC is pinned separately in `manifests/<release>-hgnc.json` and verified on
download, offline verification and mechanism transformation. Add a reviewed
snapshot pin for each new release; normal runs never replace it.

`croissant.json` supplies the publication date and licence. Only the top-level
`result` is read from the large build-log `manifest.json`; it is recorded but
does not gate ingestion. See the [26.06 packaging failure](README.md#a-trap-worth-knowing).

## 3. Target model

Biolink supplies classes and predicates; [schema/opentargets.yaml](../schema/opentargets.yaml)
defines the model. Namespace bases are in [shared/rdf.py](../shared/rdf.py) and
[common.py](common.py). `sagebrain:` is the only local namespace; acceptance
check 11 reports terms not yet defined in sagebrain-model.

Mechanisms and indications are association nodes, with typed endpoints and
`infores:open-targets` provenance. Here, “edge” refers to an association record:

```turtle
CHEMBL:CHEMBL2103875
    a biolink:SmallMolecule ;
    rdfs:label "TRAMETINIB" ;
    sagebrain:drug_type "Small molecule" ;
    sagebrain:inchi_key "LIRYPHYGHXZJBZ-UHFFFAOYSA-N" ;
    sagebrain:overall_clinical_stage "APPROVAL" ;
    skos:altLabel "GSK1120212" , "MEKINIST" .

HGNC:6840  a biolink:Gene ; rdfs:label "MAP2K1" ; biolink:in_taxon NCBITaxon:9606 .

[] a biolink:ChemicalToGeneAssociation ;
   biolink:subject CHEMBL:CHEMBL2103875 ;
   biolink:object HGNC:6840 ;
   biolink:predicate biolink:affects ;
   sagebrain:action_type "INHIBITOR" ;
   sagebrain:mechanism_of_action "Dual specificity mitogen-activated protein kinase kinase 1 inhibitor" ;
   sagebrain:target_type "single protein" ;
   biolink:original_object "ENSEMBL:ENSG00000169032" ;
   biolink:primary_knowledge_source infores:open-targets .

[] a biolink:ChemicalToDiseaseOrPhenotypicFeatureAssociation ;
   biolink:subject CHEMBL:CHEMBL1614701 ;
   biolink:object EFO:EFO_0000658 ;
   biolink:predicate biolink:treats_or_applied_or_studied_to_treat ;
   sagebrain:max_clinical_stage "APPROVAL" ;
   sagebrain:clinical_report_count 14 ;
   biolink:primary_knowledge_source infores:open-targets .
```

## 4. Modelling rules

- **Gene identity.** Resolve Ensembl targets to HGNC so they join Reactome genes
  by IRI. Preserve the Ensembl ID as a string in `biolink:original_object`.
  Multiple HGNC matches produce multiple associations. Fail when more than 10%
  of target lookups are unresolved; the threshold counts lookups, not distinct IDs.
- **Mechanisms.** Use `biolink:affects` and preserve the source action in
  `sagebrain:action_type`. Keep `sagebrain:target_type` on every association:
  family and complex members come from a group claim, not independently measured
  interactions. Filter by target type when counting drug–target interactions.
- **Indications.** Use `biolink:treats_or_applied_or_studied_to_treat` because the
  dataset includes investigational uses. Clinical stage belongs to the specific
  drug–disease pair; its maximum does not preserve trial history or stop reasons.
- **Molecule classes.** Map `Small molecule` to `biolink:SmallMolecule` and other
  modalities to `biolink:ChemicalEntity`. Preserve `drugType` as
  `sagebrain:drug_type`; finer classification is not validated by this ingest,
  and `ChemicalEntity` is a loose fit for cell therapies.
- **Disease and phenotype nodes.** Emit only terms referenced by indications.
  Type by ID prefix: MONDO uses `biolink:Disease`; HP/MP use
  `biolink:PhenotypicFeature`; GO/OBA use
  `biolink:DiseaseOrPhenotypicFeature`. See `DISEASE_NODE_CLASS` in `common.py`
  for the full mapping. Unknown IRI prefixes fail instead of producing guessed IRIs.
- **Names and parents.** Preserve synonyms and trade names as `skos:altLabel`.
  Emit `sagebrain:parent_molecule` from `parentId` so salt forms can link to
  their parent despite overlapping labels.

The two clinical-stage properties have different scopes:

| Source column | RDF property | Attached to |
|---|---|---|
| `clinical_indication.maxClinicalStage` | `sagebrain:max_clinical_stage` | Drug–disease association |
| `drug_molecule.maximumClinicalStage` | `sagebrain:overall_clinical_stage` | Molecule |

The molecule's stage is copied from the source, not computed from this graph's
indications. It does not identify a disease and is not ChEMBL's numeric `max_phase`.

## 5. Duplicates and label ambiguity

Mechanism rows contain lists of drugs and targets. Expand them, resolve genes,
then deduplicate by (ChEMBL ID, HGNC ID, action, mechanism text, target type,
Ensembl ID). Report collapsed duplicates; rows without gene targets are counted
but produce no association.

The label TSV has one row per (case-folded label, molecule), with `ambiguous=yes`
when a label names multiple molecules. RDF retains each molecule's own labels.
The ingest never selects a winning candidate; stronger name normalization and
ambiguity resolution belong to consumers.

## 6. Validation and failures

Before transformation, [verify_schemas.py](verify_schemas.py) checks required
columns, controlled vocabularies and indication ID prefixes. Added columns are
allowed; missing required columns and unknown values fail. This includes the
pinned `clinical_report` layout and stage vocabulary even though reports are not
projected. Integrity failures, unpinned inputs and excessive unresolved target
lookups also stop the pipeline.

[acceptance_checks.py](acceptance_checks.py) runs eleven checks on the loaded graph:

- Compound typing and labels; typed gene and disease/phenotype objects.
- Indication stages, action types and target types against their vocabularies.
- HGNC gene identifiers and taxon cardinality.
- Release size within `TRIPLE_RANGE`; agreement within 1% with `void:triples`
  when that metadata is present.
- Known mechanism and indication facts, including the neurofibroma anchors.
- Clinical-stage properties on the correct subjects, never both on one subject.
- Local model-term definitions.

Structural failures stop the pipeline. Model-term review only warns, including
when the model cannot be fetched. Acceptance runs after loading; a failure does
not undo the load.

## 7. Graphs and refresh

Reloading replaces `urn:sagebrain:opentargets:<release>`, retaining other releases.
The loader reads `molecules.ttl`, `mechanisms.ttl` and `indications.ttl`; keep
reports and exports outside `rdf/`.

VoID metadata lives in the default graph: source URL, release anchor, licence,
citation, triple count and comments explaining stage scope, indication semantics,
group targets and gene identity.

## 8. Open decisions

- **Structure-based resolution:** InChIKey and SMILES are emitted; a resolver
  using them is not implemented.
- **Cross-references:** `crossReferences` (PubChem, DrugBank, etc.) is unused and
  therefore absent from the required-column gate.
