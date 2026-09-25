# `drug_molecule`

ChEMBL molecules: the node layer the rest of the drug graph hangs off. One Spark
part (`part-00000-5581d2c3-…`), **22,407 rows, 13 columns**. Every row is one ChEMBL
ID; `id` is unique.

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

A node per molecule — `biolink:SmallMolecule` for the 18,124 small molecules,
`biolink:ChemicalEntity` for the other ten modalities — plus the preferred name,
every synonym and trade name as `skos:altLabel`, structures, modality, overall stage
and parent. Separately, `exports/chembl_labels.tsv`: 101,195 (label, molecule) rows,
97,835 distinct folded labels, 2,607 of them ambiguous. Three synonyms are dropped
for carrying a NUL where the source meant a registered-trademark sign; they are
listed in `reports/rejected_labels.tsv`.

## Quirks

**16.6% of molecules have no structure.** `inchiKey`, `canonicalSmiles` and `molblock`
share the same 83.4% fill — they are present or absent together. The 3,710 without are
the biologics and cell therapies, so any structure-based resolver is small-molecule only.

**211 parent references dangle.** 1,999 molecules carry a `parentId`, but only 1,788 of
those parents are rows in this file. `sagebrain:parent_molecule` therefore emits 211
IRIs that have no node in the graph. `childChemblIds` is the inverse relation and is
filled on only 3,676 rows, so it does not close the gap.

**`name` is not distinct.** 22,371 distinct names over 22,407 molecules, and on 1,473
rows the preferred name is repeated inside `synonyms`. The label export folds case and
records ambiguity rather than picking a winner.

**`description` is templated.** 100% filled but only 3,361 distinct values, so it is
generated prose rather than a per-molecule annotation. Unused.

**`crossReferences` is the useful unused column** — 18,000 molecules carry one, led by
`drugbank` (11,160), `Probes&Drugs` (4,569), `DailyMed` (1,542), `USAN` (1,384) and
`INN` (1,124). It is deliberately outside the gate; see the open decisions in
[DESIGN.md](../DESIGN.md).
