# `drug_mechanism_of_action`

ChEMBL mechanism rows: what a drug does to which gene. Two Spark parts,
**6,500 rows, 7 columns**. Emitted by
[`transform_mechanisms.py`](../transform_mechanisms.py).

**The dataset has no key column.** There is no `id`, and a row is a
(drugs × targets × action × mechanism text × target type) bundle rather than an edge,
so identity has to be reconstructed from the values. That is why deduplication is on
the full tuple rather than on an upstream identifier.

## Columns

All six read columns are in the layout gate; `references` is the only unused one.

| Column | Type | Fill | Read |
|---|---|---:|---|
| `chemblIds` | list\<string\> | 100%, max 2 | yes — fanned out, one edge per drug |
| `targets` | list\<string not null\> | 5,764 non-empty, max 78 | yes — Ensembl, resolved to HGNC |
| `actionType` | string | 100%, 30 values | yes — `sagebrain:action_type` |
| `targetType` | string | 100%, 9 values | yes — `sagebrain:target_type` |
| `mechanismOfAction` | string | 100%, 1,725 distinct | yes — `sagebrain:mechanism_of_action` |
| `targetName` | string | 100%, 1,295 distinct | yes — `sagebrain:target_name` |
| `references` | list\<struct\<source, ids\>\> | 6,462 non-empty | no |

## Vocabularies

`actionType` has 30 values (`ACTION_TYPES`), led by INHIBITOR (3,379), ANTAGONIST
(978), AGONIST (948), BINDING AGENT (253) and BLOCKER (179). They are emitted verbatim
rather than mapped to Biolink predicates — `OPENER` and `STABILISER` have no faithful
equivalent, so every edge uses `biolink:affects` and carries the source word.

`targetType` has 9 values (`TARGET_TYPES`), and the split is load-bearing:

| Value | Rows | | Value | Rows |
|---|---:|---|---|---:|
| single protein | 4,884 | | nucleic-acid | 74 |
| protein family | 753 | | chimeric protein | 23 |
| protein complex | 368 | | selectivity group | 12 |
| protein complex group | 292 | | protein-protein interaction | 9 |
| protein nucleic-acid complex | 85 | | | |

Only `single protein`, `chimeric protein` and `nucleic-acid` (`SINGLE_TARGET_TYPES`)
mean one drug measured against one gene. For the rest, `targets` enumerates a named
group's **members** — trametinib's "MEK1/2" row lists MAP2K1 and MAP2K2 — so the edge
asserts group membership, not an independently measured interaction.

## What the ingest emits

14,708 `ChemicalAffectsGeneAssociation` edges over 1,548 HGNC gene nodes. Group-membership
edges outnumber single-protein edges **9,506 to 5,202**, so any count of "drug–target
interactions" has to filter on `sagebrain:target_type` or it inflates by ~1.8×.

Ensembl IDs resolve through the separately pinned HGNC snapshot: 15,397 of 15,404
lookups resolve, 2 distinct IDs unresolved. The Ensembl ID stays on the edge as
`biolink:original_object`.

## Quirks

**736 rows have no gene target** (11.3%) — vaccine antigens and some cell therapies.
Counted and reported, never emitted: an edge needs two endpoints.

**689 duplicate edges are collapsed.** Only one pair of rows is byte-identical
upstream; the rest of the duplication appears *after* fanning out `chemblIds` ×
`targets` × resolved HGNC, where two different rows land on the same tuple.

**One row can carry 78 targets.** The long tail is protein families, and each member
becomes its own edge, which is the main reason 6,500 rows produce 14,708 edges.

**`references` is unused** — 6,462 rows carry provenance (PubMed 4,409, Other 1,053,
DailyMed 926, FDA 827, Wikipedia 545, ISBN 282). Edges currently assert
`infores:open-targets` as the single knowledge source.
