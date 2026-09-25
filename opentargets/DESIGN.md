# Open Targets design decisions

This document records the drug-layer model and validation contract. See
[README.md](README.md) for operations and release statistics,
[TRIALS.md](TRIALS.md) for the trial layer's scope record,
[datasets/](datasets/) for per-dataset source notes, and
[manifests/](manifests/) for recorded results.

## 1. Release contents and ingest scope

Open Targets Platform 26.06 is a collection of related datasets. Its
`release_data_integrity` inventory lists 56 directories under
`output/`, covering targets, diseases, drugs, clinical records, genetic evidence,
association scores and supporting annotations. This ingest downloads five of
those datasets as Parquet, and projects all five; each can contain one file or
several parts.

The selected inputs are listed below. Counts are **source rows in release 26.06**,
from the [source manifest](manifests/26.06-sources.tsv), not emitted edge counts.
The [column report](manifests/26.06-column-verification.md) records their layouts.

| Source dataset | Rows | What the source contains | What this ingest emits |
|---|---:|---|---|
| [`drug_molecule`](datasets/drug_molecule.md) | 22,407 | ChEMBL IDs, names, synonyms, trade names, modality, structures, parent molecule and overall clinical stage | Molecule nodes and properties; a separate label TSV |
| [`drug_mechanism_of_action`](datasets/drug_mechanism_of_action.md) | 6,500 | Lists of ChEMBL drugs and Ensembl targets, action, mechanism text and target type/name | Drug–gene associations and HGNC gene nodes; rows expand across drugs and resolved targets |
| [`clinical_indication`](datasets/clinical_indication.md) | 86,468 | Drug–disease pairs, maximum clinical stage and supporting report IDs | Indication associations with stage and report count |
| [`disease`](datasets/disease.md) | 47,080 | MONDO, EFO and other disease/phenotype IDs, names, synonyms, ontology ancestry, cross-references and therapeutic areas | Typed nodes, labels and synonyms for the 4,059 terms referenced by indications or trials; no ontology hierarchy |
| [`clinical_report`](datasets/clinical_report.md) | 289,955 | Trials, labels and regulatory records, including stage, drugs, diseases, trial dates, stop reasons and review information | Trial nodes with stage, overall status, start month, stop reasons and quality-control flags, linked to compounds and diseases; only `type = CLINICAL_TRIAL` and only where a drug resolved to ChEMBL — see [TRIALS.md](TRIALS.md) |

Open Targets uses an [EFO-based disease/phenotype ontology](https://platform-docs.opentargets.org/disease-or-phenotype)
that includes MONDO terms. In 26.06, 70,521 of 86,468 indication rows (about 82%)
reference MONDO IDs; others use HP (HPO), EFO, Orphanet, MP and additional prefixes
listed in the [prefix audit](manifests/26.06-column-verification.md#disease-id-prefixes-used-by-clinical_indication).
The ingest preserves these source identifiers rather than normalizing all terms
to MONDO.

ChEMBL IDs connect molecules to mechanisms, indications and trials; disease IDs
connect indications and trials to term labels. Mechanism targets are Ensembl IDs,
resolved through a separately pinned HGNC snapshot so they share gene identity with
the Reactome ingest and join its gene nodes directly by IRI. The indication
transform counts `clinicalReportIds` directly and does not emit them, so an
indication edge and the trial nodes behind it are joined by a consumer on
(drug, disease) rather than by an asserted link; the count is also over all four
record kinds while trial nodes are trials only.

The remaining 51 output directories are currently not downloaded or projected by the
default pipeline, though the pipeline may additionally tap these later. Examples from the pinned release inventory:

| Excluded data | Dataset examples | Scope decision |
|---|---|---|
| Target–disease scores | `association_overall_*`, `association_by_datasource_*`, `association_by_datatype_*` | Outside the initial drug-layer scope; may be added later for ranking, alongside the evidence they summarize |
| Supporting evidence and genetics | `evidence_*`, `variant`, `study`, `credible_set`, `colocalisation`, `l2g_prediction` | Separate evidence and variant models would be needed |
| Target and biological annotations | `target`, `target_essentiality`, `target_prioritisation`, `baseline_expression`, `interaction`, `reactome`, `go` | This pass emits only the gene nodes needed by mechanisms |
| Derived drug–target–disease join | `clinical_target` | Reproducible from datasets already ingested, and with a coarser clinical stage |
| Additional clinical and drug data | `drug_warning`, `pharmacogenomics`, `openfda_significant_adverse_drug_reactions` | Outside the current molecule/mechanism/indication/trial model |

`clinical_target` is excluded on different grounds than the rest since it is somewhat
redundant with what the release already builds from `clinical_report` and
`drug_mechanism_of_action` under the same report QC filter as `clinical_indication`;
its drug–target–disease triples are exactly the join of those two datasets on
drug. Its `maxClinicalStage` is scoped to the drug–target pair, so reaching the same
claim through the drug keeps the more precise per-pair `sagebrain:max_clinical_stage`.
The trials it adds, whose diseases never mapped to an ontology term, are already
projected from `clinical_report` as drug-only trial nodes.

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
    trials["clinical_report + disease<br/>Transform trials and trial-only terms"]
    dropped["clinical_report, 96,486 rows out of scope<br/>Labels, curated resources, regulatory records,<br/>and trials with no ChEMBL-resolved drug"]
    hgnc["HGNC snapshot<br/>Verify separate release pin"]
    labels["drug_molecule<br/>Export label TSV outside RDF"]

    gate --> molecules
    gate --> mechanisms
    gate --> indications
    gate --> trials
    gate --> labels
    trials -.-> dropped
    hgnc --> mechanisms

    load["Load into Oxigraph<br/>Release graph + default-graph VoID metadata"]
    molecules -->|molecules.ttl| load
    mechanisms -->|mechanisms.ttl| load
    indications -->|indications.ttl| load
    trials -->|trials.ttl| load
    load --> checks["Run acceptance checks<br/>on the loaded graph"]

    classDef excluded fill:#f0f0f0,stroke:#666,color:#333;
    class omitted excluded;
    class dropped excluded;
```

The diagram shows dependencies; [pipeline.py](pipeline.py) runs the transforms
and label export sequentially. The four transforms read the verified source files
independently, then the loader combines their Turtle outputs. Indications and
trials split the disease pass between them — each works the split out from the
source columns, not from the other's output — so even those two can run in either
order.

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

Biolink supplies classes and predicates, and every `biolink:` term emitted must
be defined in the pinned release — it is a published vocabulary, not ours to
mint, so an undefined term is a typo or a term that moved and the graph is wrong
now. That is the opposite verdict from a `sagebrain:` term, which only warns.
Two association classes were carried for several releases under names Biolink
never had; see the note in the schema. Acceptance check 20 now enforces this
against the release named by `settings.biolink_version`, so it cannot recur
silently.
[schema/opentargets.yaml](../schema/opentargets.yaml) defines the model. Namespace bases are in [shared/rdf.py](../shared/rdf.py) and
[common.py](common.py). `sagebrain:` is the only local namespace; acceptance
check 18 reports terms not yet defined in sagebrain-model.

Mechanisms and indications are association nodes, with typed endpoints and
`infores:open-targets` provenance. Here, “edge” refers to an association record.
Trials are plain nodes, not associations: a trial is a study that happened, and
the drugs and diseases are its properties rather than a claim it makes.

```turtle
CHEMBL:CHEMBL2103875
    a biolink:SmallMolecule ;
    rdfs:label "TRAMETINIB" ;
    sagebrain:drug_type "Small molecule" ;
    sagebrain:inchi_key "LIRYPHYGHXZJBZ-UHFFFAOYSA-N" ;
    sagebrain:overall_clinical_stage "APPROVAL" ;
    skos:altLabel "GSK1120212" , "MEKINIST" .

HGNC:6840  a biolink:Gene ; rdfs:label "MAP2K1" ; biolink:in_taxon NCBITaxon:9606 .

[] a biolink:ChemicalAffectsGeneAssociation ;
   biolink:subject CHEMBL:CHEMBL2103875 ;
   biolink:object HGNC:6840 ;
   biolink:predicate biolink:affects ;
   sagebrain:action_type "INHIBITOR" ;
   sagebrain:mechanism_of_action "Dual specificity mitogen-activated protein kinase kinase 1 inhibitor" ;
   sagebrain:target_type "single protein" ;
   biolink:original_object "ENSEMBL:ENSG00000169032" ;
   biolink:primary_knowledge_source infores:open-targets .

[] a biolink:ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation ;
   biolink:subject CHEMBL:CHEMBL1614701 ;
   biolink:object EFO:EFO_0000658 ;
   biolink:predicate biolink:treats_or_applied_or_studied_to_treat ;
   sagebrain:max_clinical_stage "APPROVAL" ;
   sagebrain:clinical_report_count 14 ;
   biolink:primary_knowledge_source infores:open-targets .

CLINICALTRIALS:NCT02407405
    a biolink:ClinicalTrial ;
    rdfs:label "Phase II Trial of the MEK1/2 Inhibitor Selumetinib ..." ;
    sagebrain:trial_clinical_stage "PHASE_2" ;
    biolink:clinical_trial_overall_status "ACTIVE_NOT_RECRUITING" ;
    sagebrain:trial_start_date "2016-01"^^xsd:gYearMonth ;
    sagebrain:trial_drug CHEMBL:CHEMBL1614701 ;
    biolink:clinical_trial_conditions EFO:EFO_0000658 .
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
  drug–disease pair; its maximum does not preserve trial history or stop reasons —
  the trial layer is where that history lives.
- **Trials.** Emit as nodes keyed on the ClinicalTrials.gov accession, the only
  identifier in `clinical_report` that survives a release, and only for
  `type = CLINICAL_TRIAL` records naming a ChEMBL-resolved drug. Use Biolink where
  its declared range fits — `biolink:clinical_trial_conditions`, and
  `biolink:clinical_trial_overall_status`, whose `ClinicalTrialStatusEnum` matches
  Open Targets' 13 statuses value-for-value — and a local slot where it does not
  (`sagebrain:trial_drug`, because `clinical_trial_interventions` has range
  `clinical intervention`). Start dates are `xsd:gYearMonth`, since the source's
  day is manufactured for older records. Keep the status: it is what distinguishes
  a trial that halted midway from one that never enrolled anyone, and a stop
  reason alone cannot. See [TRIALS.md](TRIALS.md).
- **Molecule classes.** Map `Small molecule` to `biolink:SmallMolecule` and other
  modalities to `biolink:ChemicalEntity`. Preserve `drugType` as
  `sagebrain:drug_type`; finer classification is not validated by this ingest,
  and `ChemicalEntity` is a loose fit for cell therapies.
- **Disease and phenotype nodes.** Emit only terms referenced by an indication or
  a trial — 4,059 of the release's 47,080. Each term is asserted exactly once:
  `indications.ttl` owns the indication-referenced terms and `trials.ttl` owns
  the 310 that only a trial references. Type by ID prefix: MONDO uses
  `biolink:Disease`; HP/MP use `biolink:PhenotypicFeature`; GO/OBA use
  `biolink:DiseaseOrPhenotypicFeature`. See `DISEASE_NODE_CLASS` in `common.py`
  for the full mapping. Unknown IRI prefixes fail instead of producing guessed IRIs.
- **Names and parents.** Preserve synonyms and trade names as `skos:altLabel`.
  Emit `sagebrain:parent_molecule` from `parentId` so salt forms can link to
  their parent despite overlapping labels.

The three clinical-stage properties have different scopes, narrowest last:

| Source column | RDF property | Attached to |
|---|---|---|
| `drug_molecule.maximumClinicalStage` | `sagebrain:overall_clinical_stage` | Molecule |
| `clinical_indication.maxClinicalStage` | `sagebrain:max_clinical_stage` | Drug–disease association |
| `clinical_report.clinicalStage` | `sagebrain:trial_clinical_stage` | Trial |

The molecule's stage is copied from the source, not computed from this graph's
indications. It does not identify a disease and is not ChEMBL's numeric `max_phase`.
Acceptance check 10 fails if any subject carries more than one of the three, which
is the failure mode that would produce a plausible wrong answer rather than an
obvious one.

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
allowed; missing required columns and unknown values fail. This includes `clinical_report.type` — the trial layer's scope filter, gated
so a renamed or added record kind cannot change what the layer contains without
saying so — and the two list-valued columns the trial layer emits, flattened first
and audited across all 289,955 rows rather than only the trials in scope.
Ten vocabularies are audited in total. Integrity failures, unpinned inputs,
excessive unresolved target lookups and more than 1% implausible trial start dates
also stop the pipeline.

[acceptance_checks.py](acceptance_checks.py) runs twenty-one checks on the loaded
graph:

- Compound typing and labels; typed gene and disease/phenotype objects.
- Indication stages, action types and target types against their vocabularies.
- HGNC gene identifiers and taxon cardinality.
- Release size within `TRIPLE_RANGE`; agreement within 1% with `void:triples`
  when that metadata is present.
- Known mechanism and indication facts, including the neurofibroma anchors, and
  known trial facts down to the stop-reason categories.
- Clinical-stage properties on the correct subjects, never two on one subject.
- Trial nodes registry-keyed and staged; trial drug and condition links resolving
  to asserted nodes; trial vocabularies; start dates typed `xsd:gYearMonth` and
  inside the plausible window; stop reasons only on `TERMINATED`, `WITHDRAWN` or
  `SUSPENDED` trials, each carrying exactly one status.
- Biolink terms defined in the pinned release, and local model-term definitions.
  The two namespaces reach opposite verdicts from one scan of the graph: an
  undefined `biolink:` term FAILS, because Biolink is published on a pinned
  version and a term it does not define is a typo or one that moved; an
  unratified `sagebrain:` term only warns, because it is a to-do for the model
  repo. An unreachable Biolink or model warns either way. Pass
  `--biolink-yaml` to check against a working copy or run offline.

Structural failures stop the pipeline. Model-term review only warns, including
when the model cannot be fetched. Acceptance runs after loading; a failure does
not undo the load.

## 7. Graphs and refresh

Reloading replaces `urn:sagebrain:opentargets:<release>`, retaining other releases.
The loader reads `molecules.ttl`, `mechanisms.ttl`, `indications.ttl` and
`trials.ttl`; keep reports and exports outside `rdf/`.

VoID metadata lives in the default graph: source URL, release anchor, licence,
citation, triple count and comments explaining stage scope, indication semantics,
group targets, gene identity, what the trial layer's scope filters exclude, and why
trial start dates carry month precision.

## 8. Open decisions

- **Structure-based resolution:** InChIKey and SMILES are emitted; a resolver
  using them is not implemented.
- **Cross-references:** `crossReferences` (PubChem, DrugBank, etc.) is unused and
  therefore absent from the required-column gate.
- **Indication-to-trial links:** `clinicalReportIds` is still not emitted, so the
  graph does not state which trials an indication edge was computed from. A
  consumer joins on (drug, disease), which is nearly but not exactly the same set.
- **Trial literature:** `trialLiterature` (337,018 PMIDs over 83,501 rows) is read
  by nothing and is therefore absent from the required-column gate, as
  `crossReferences` is. It would let a trial node cite the publications reporting
  it, and is the obvious next addition to the trial layer.
