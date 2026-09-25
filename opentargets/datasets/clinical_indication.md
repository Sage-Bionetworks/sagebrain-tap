# `clinical_indication`

Drug–disease pairs with the highest clinical stage reached. One file,
**86,468 rows, 5 columns**. Emitted by
[`transform_indications.py`](../transform_indications.py).

The cleanest dataset in the release: every column is 100% filled, and
`(drugId, diseaseId)` is unique across all 86,468 rows — no deduplication is needed
and none happens.

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
(15,850), APPROVAL (11,175), UNKNOWN (10,218), PHASE_1 (9,637), PHASE_1_2 (6,443) and
a short tail. `PHASE_4` and `WITHDRAWAL` never appear here.

## Disease ID prefixes

The object axis is an EFO-based mix of ontologies, not MONDO alone:

| Prefix | Rows | | Prefix | Rows |
|---|---:|---|---|---:|
| MONDO | 70,521 | | MP | 430 |
| HP | 8,591 | | GO | 154 |
| EFO | 6,300 | | OBA | 35 |
| Orphanet | 432 | | NCIT / OTAR | 5 |

HP and MP are phenotypes, so `DISEASE_NODE_CLASS` types nodes from the prefix rather
than calling everything a disease. An unknown prefix fails the run instead of
producing a guessed IRI.

## What the ingest emits

86,468 `ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation` edges over 11,364 drugs ×
3,749 diseases, 11,175 of them at APPROVAL. The predicate is
`biolink:treats_or_applied_or_studied_to_treat`, deliberately weaker than
`biolink:treats`: 75,293 rows sit below APPROVAL, and a phase-1 trial is a compound
being studied, not one that treats.

Labels and synonyms for the 3,749 referenced terms come from
[`disease`](disease.md).

## Quirks

**Report IDs are counted, not emitted.** `clinicalReportIds` becomes
`sagebrain:clinical_report_count` — a stage backed by 14 reports and one backed by a
single record are not equally load-bearing. The lists are long (up to 856) and their
147,845 distinct IDs **all resolve** into `clinical_report`, with zero dangling
references, so the count can become real edges whenever the trial layer lands. See
[TRIALS.md](../TRIALS.md).

**Stage belongs to the pair, never the drug.** A drug that failed phase 3 for one
disease and was approved for another carries both, on different edges.
`drug_molecule.maximumClinicalStage` is the drug-level maximum and is emitted under a
deliberately different name, `sagebrain:overall_clinical_stage`.

**`id` is gated but unread.** It is in `required_columns` and not in the transform's
column list — harmless, but the gate is wider than the read here.
