# `clinical_indication`

Drug–disease pairs with the highest clinical stage reached. Release 26.06 contains
**86,468 rows and 5 columns** in a single file. Emitted by
[`transform_indications.py`](../transform_indications.py).

Every column is populated, and `(drugId, diseaseId)` is unique. The transform supports
deduplication, but no duplicate associations are removed in this release.

Upstream builds it from [`clinical_report`](clinical_report.md) under a QC filter that
drops `PHASE_IV_NOT_APPROVED` and `INDIRECT_PRIMARY_PURPOSE` records.

## Columns

| Column | Type | Fill | Read |
|---|---|---:|---|
| `drugId` | large_string | 100%, 11,364 distinct | yes — subject |
| `diseaseId` | large_string | 100%, 3,749 distinct | yes — object |
| `maxClinicalStage` | large_string | 100%, 11 values | yes — `sagebrain:max_clinical_stage` |
| `clinicalReportIds` | large_list\<large_string\> | 100%, max 856 | counted only |
| `id` | large_string | 100%, unique | no — in the gate, not read |

## Vocabulary

`maxClinicalStage` uses 11 of the 13 `CLINICAL_STAGES`: PHASE_2 (28,370), PHASE_3
(15,850), APPROVAL (11,175), UNKNOWN (10,218), PHASE_1 (9,637), PHASE_1_2 (6,443) and a
short tail. `PHASE_4` and `WITHDRAWAL` never appear here.

## Disease ID prefixes

Disease and phenotype identifiers come from an EFO-based collection of ontologies:

| Prefix | Rows | | Prefix | Rows |
|---|---:|---|---|---:|
| MONDO | 70,521 | | MP | 430 |
| HP | 8,591 | | GO | 154 |
| EFO | 6,300 | | OBA | 35 |
| Orphanet | 432 | | NCIT / OTAR | 5 |

`DISEASE_NODE_CLASS` maps identifier prefixes to node classes, including phenotype
classes for HP and MP. An unknown identifier prefix fails validation.

## What the ingest emits

86,468 `ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation` edges over
11,364 drugs × 3,749 diseases, 11,175 of them at APPROVAL. The predicate is
`biolink:treats_or_applied_or_studied_to_treat`, which includes investigational uses. Of
these associations, 75,293 have stages below APPROVAL; using `biolink:treats` would
overstate their meaning.

Labels and synonyms for the 3,749 referenced terms come from [`disease`](disease.md).

One `drugId` — `CHEMBL453514`, an APPROVAL for MONDO_0005113 — has no row in
[`drug_molecule`](drug_molecule.md). The transform emits a typed, unlabeled
`biolink:ChemicalEntity` stub for this reference. The missing `sagebrain:drug_type`
distinguishes it from described molecules. Acceptance checks require all association
subjects to resolve to typed compound nodes.

## Data characteristics

**Report IDs are counted, not emitted.** `clinicalReportIds` becomes
`sagebrain:clinical_report_count`. Lists contain up to 856 entries, and all 147,845
distinct report IDs resolve to `clinical_report`. Explicit report links remain outside
the projection; see [TRIALS.md](../TRIALS.md).

**Stage belongs to the pair, never the drug.** A drug that failed phase 3 for one
disease and was approved for another carries both, on different edges.
`drug_molecule.maximumClinicalStage` is the source-supplied molecule-level stage,
emitted as `sagebrain:overall_clinical_stage`. It cannot be derived reliably from the
indication associations.

**`id` is gated but unread.** It is in `required_columns` and not in the transform's
column list, so required-column validation includes an unused field.
