# `disease`

The EFO-based disease and phenotype ontology used by indications and trials. Release
26.06 contains **47,080 rows and 18 columns** in a single file.
[`transform_indications.py`](../transform_indications.py) and
[`transform_trials.py`](../transform_trials.py) use its labels and exact synonyms.

The graph includes 4,059 referenced terms: 3,749 used by indications and 310 additional
terms used only by trials. Ontology relationships are not projected.

## Columns

Six columns are required by layout validation. The transforms read identifiers, names,
exact synonyms, and therapeutic areas; therapeutic areas are not emitted.

| Column | Type | Fill | Read |
|---|---|---:|---|
| `id` | large_string | 100%, unique | yes — CURIE |
| `name` | large_string | 100%, unique | yes — `rdfs:label` |
| `exactSynonyms` | large_list | 34,965 non-empty, max 67 | yes — `skos:altLabel` |
| `therapeuticAreas` | large_list | 47,080 non-empty, 26 distinct | read, **not emitted** |
| `dbXRefs` | large_list | 39,015 non-empty, max 47 | no — gated, not read |
| `ancestors` | large_list | 47,055 non-empty, max 65 | no — gated, not read |
| `code` | large_string | 100% | no |
| `description` | large_string | 88.5% | no |
| `parents` / `children` | large_list | 47,055 / 8,375 non-empty | no |
| `descendants` | large_list | 8,375 non-empty, max 25,048 | no |
| `relatedSynonyms` | large_list | 7,249 non-empty | no |
| `narrowSynonyms` / `broadSynonyms` | large_list | 600 / 699 non-empty | no |
| `obsoleteTerms` / `obsoleteXRefs` | large_list | 7,543 / 4,463 non-empty | no |
| `ontology` | struct | 100% | no |
| `synonyms` | struct | 100% | no |

## Term prefixes

The source ontology covers more terms than the projection references:

| Prefix | Terms | | Prefix | Terms |
|---|---:|---|---|---:|
| OBA | 17,441 | | Orphanet | 2,035 |
| MONDO | 15,489 | | GO | 477 |
| EFO | 9,278 | | GSSO / OTAR / OBI | 29 |
| HP | 2,322 | | | |

`OBA` is the largest prefix in the file, but indications reference few of its terms. The
source serves broader ontology use cases beyond this projection.

## What the ingest emits

The indication and trial transforms emit referenced terms with classes selected by
`DISEASE_NODE_CLASS`, names as `rdfs:label`, and exact synonyms as `skos:altLabel`. The
shared label map is loaded from this dataset before each transform scans its references.

## Data characteristics

**No ontology hierarchy is emitted.** `parents`, `children`, `ancestors` and
`descendants` are all present and all dropped, so the disease nodes are a flat
vocabulary. `descendants` runs to 25,048 entries on one term — projecting the hierarchy
would materially increase the projection size.

**`therapeuticAreas` is read and discarded.** The transform loads it into its label map
without emitting it. The source includes 26 distinct areas.

**`dbXRefs` and `ancestors` are gated but never read.** They sit in `required_columns`
while the transform reads only `id`, `name`, `exactSynonyms` and `therapeuticAreas`, so
required-column validation includes unused fields.

**`id` and `code` are never equal.** `code` is the full PURL
(`http://purl.obolibrary.org/obo/GO_0000050`), `id` the short form (`GO_0000050`). Only
`id` is used.

**`ontology` and `synonyms` are structs that restate flat columns**, so the same synonym
data is present twice in two shapes.
