# `drug_mechanism_of_action`

ChEMBL mechanism records describing compound actions and targets. Release 26.06 contains
**6,500 rows and 7 columns** across two Spark parts. Emitted by
[`transform_mechanisms.py`](../transform_mechanisms.py).

The dataset has no `id` column. Rows contain lists of drugs and targets together with
action, mechanism text, and target type. The transform expands these lists and
deduplicates the resulting associations by their content.

## Columns

All six read columns are in the layout gate; `references` is the only unused one.

| Column | Type | Fill | Read |
|---|---|---:|---|
| `chemblIds` | list\<string\> | 100%, max 2 | yes — expanded across drugs and targets |
| `targets` | list\<string not null\> | 5,764 non-empty, max 78 | yes — Ensembl, resolved to HGNC |
| `actionType` | string | 100%, 30 values | yes — `sagebrain:action_type` |
| `targetType` | string | 100%, 9 values | yes — `sagebrain:target_type` |
| `mechanismOfAction` | string | 100%, 1,725 distinct | yes — `sagebrain:mechanism_of_action` |
| `targetName` | string | 100%, 1,295 distinct | yes — `sagebrain:target_name` |
| `references` | list\<struct\<source, ids\>\> | 6,462 non-empty | no |

## Vocabularies

`actionType` has 30 values (`ACTION_TYPES`), led by INHIBITOR (3,379), ANTAGONIST (978),
AGONIST (948), BINDING AGENT (253) and BLOCKER (179). They are emitted verbatim
alongside `biolink:affects`. The vocabulary does not map consistently to more specific
Biolink predicates.

`targetType` has 9 values, validated against `TARGET_TYPES`:

| Value | Rows | | Value | Rows |
|---|---:|---|---|---:|
| single protein | 4,884 | | nucleic-acid | 74 |
| protein family | 753 | | chimeric protein | 23 |
| protein complex | 368 | | selectivity group | 12 |
| protein complex group | 292 | | protein-protein interaction | 9 |
| protein nucleic-acid complex | 85 | | | |

Only `single protein`, `chimeric protein` and `nucleic-acid` (`SINGLE_TARGET_TYPES`) are
classified as individual targets by this ingest. For other types, `targets` enumerates
members of a named group. For example, trametinib's MEK target group lists MAP2K1 and
MAP2K2; the resulting associations share a group-level claim.

## What the ingest emits

The transform emits 14,708 `ChemicalAffectsGeneAssociation` records and 1,548 HGNC gene
nodes. Of these associations, 9,506 derive from group targets and 5,202 from individual
targets. Filter on `sagebrain:target_type` when counting individual drug–target
interactions.

Ensembl IDs resolve through the separately pinned HGNC snapshot: 15,397 of 15,404
lookups resolve, 2 distinct IDs unresolved. The Ensembl ID stays on the edge as
`biolink:original_object`.

## Data characteristics

**736 rows have no gene target** (11.3%) — vaccine antigens and some cell therapies.
These rows are counted but produce no association.

**689 duplicate edges are collapsed.** Only one pair of rows is byte-identical upstream;
the rest of the duplication appears *after* expanding `chemblIds` × `targets` × resolved
HGNC, where two different rows land on the same tuple.

**One row can carry 78 targets.** The long tail is protein families, and each member
becomes its own edge, which is the main reason 6,500 rows produce 14,708 edges.

**`references` is unused** — 6,462 rows carry provenance (PubMed 4,409, Other 1,053,
DailyMed 926, FDA 827, Wikipedia 545, ISBN 282). Edges currently assert
`infores:open-targets` as the single knowledge source.
