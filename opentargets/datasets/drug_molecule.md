# `drug_molecule`

ChEMBL molecule records used by the mechanism, indication, and trial projections.
Release 26.06 contains **22,407 rows and 13 columns** in a single Spark part. Each row
has a unique ChEMBL `id`.

Emitted by [`transform_molecules.py`](../transform_molecules.py) and
[`export_label_index.py`](../export_label_index.py).

## Columns

Nine are in the layout gate; the other four are present and unused.

| Column | Type | Fill | Read |
|---|---|---:|---|
| `id` | string | 100% | yes — ChEMBL CURIE |
| `name` | string | 100% | yes — `rdfs:label` |
| `drugType` | string | 100% | yes — class + `sagebrain:drug_type` |
| `maximumClinicalStage` | string | 100% | yes — `sagebrain:overall_clinical_stage` |
| `synonyms` | list\<struct\<label, source\>\> | 15,369 non-empty | yes — `skos:altLabel` |
| `tradeNames` | list\<struct\<label, source\>\> | 3,787 non-empty | yes — `skos:altLabel` |
| `inchiKey` | string | 83.4% | yes — `sagebrain:inchi_key` |
| `canonicalSmiles` | string | 83.4% | yes — `sagebrain:canonical_smiles` |
| `parentId` | string | 8.9% | yes — `sagebrain:parent_molecule` |
| `molblock` | string | 83.4% | no |
| `crossReferences` | list\<struct\<source, ids\>\> | 18,000 non-empty | no |
| `childChemblIds` | list\<string\> | 3,676 non-empty | no |
| `description` | string | 100% | no |

## Vocabularies

`drugType`, all 11 values, enforced as `DRUG_TYPES`:

| Value | Rows | | Value | Rows |
|---|---:|---|---|---:|
| Small molecule | 18,124 | | Antibody drug conjugate | 108 |
| Unknown | 1,668 | | Enzyme | 107 |
| Antibody | 990 | | Vaccine component | 78 |
| Protein | 847 | | Cell | 67 |
| Oligonucleotide | 189 | | Oligosaccharide | 63 |
| Gene | 166 | | | |

`maximumClinicalStage` uses 11 of the 13 values in `CLINICAL_STAGES`; `PHASE_4` and
`WITHDRAWAL` appear only in [`clinical_report`](clinical_report.md).

## What the ingest emits

Each molecule becomes a `biolink:SmallMolecule` or `biolink:ChemicalEntity` node with a
preferred name, alternative labels, available structures, modality, overall stage, and
parent reference. Alternative labels include source synonyms and trade names other than
the preferred name. Separately, `exports/chembl_labels.tsv`: 101,195 (label, molecule)
rows, 97,835 distinct folded labels, 2,607 of them ambiguous. Three labels containing
NUL characters are excluded and listed in `reports/rejected_labels.tsv`.

## Data characteristics

**16.6% of molecules have no structure.** `inchiKey`, `canonicalSmiles` and `molblock`
have matching coverage: they are present or absent together. Structure-based resolution
cannot cover records lacking these fields, including biologics and cell therapies.

**211 parent references have no corresponding molecule record.** 1,999 molecules carry a
`parentId`, but only 1,788 of those parents are rows in this file.
`sagebrain:parent_molecule` therefore emits 211 IRIs without descriptive molecule nodes
in the graph. `childChemblIds` is the inverse relation and is filled on only 3,676 rows,
so it does not close the gap.

**`name` is not distinct.** 22,371 distinct names over 22,407 molecules, and on 1,473
rows the preferred name is repeated inside `synonyms`. The label export folds case and
records all candidate molecules for ambiguous labels.

**`description` is templated.** 100% filled but only 3,361 distinct values, so it is
generated prose rather than a per-molecule annotation. Unused.

**`crossReferences` is outside the projection.** 18,000 molecules carry one, led by
`drugbank` (11,160), `Probes&Drugs` (4,569), `DailyMed` (1,542), `USAN` (1,384) and
`INN` (1,124). It is outside required-column validation; see the open decisions in
[DESIGN.md](../DESIGN.md).
