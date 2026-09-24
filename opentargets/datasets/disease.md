# `disease`

The EFO-based disease and phenotype ontology behind the indication axis. One file,
**47,080 rows, 18 columns** — the widest dataset ingested and the most sparsely used.
Read by [`transform_indications.py`](../transform_indications.py) for labels only.

**Only 3,749 terms (8%) reach the graph** — the ones an indication actually
references. The rest describe an ontology this ingest does not claim to serve.

## Columns

Six are in the layout gate, four are read, two are emitted.

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

The file is much broader than the indication axis that uses it:

| Prefix | Terms | | Prefix | Terms |
|---|---:|---|---|---:|
| OBA | 17,441 | | Orphanet | 2,035 |
| MONDO | 15,489 | | GO | 477 |
| EFO | 9,278 | | GSSO / OTAR / OBI | 29 |
| HP | 2,322 | | | |

`OBA` is the largest prefix in the file and almost entirely unreferenced by
indications, which is the clearest sign that this dataset is an ontology dump rather
than a curated indication vocabulary.

## What the ingest emits

Nothing on its own. It supplies `rdfs:label` and `skos:altLabel` for the 3,749 terms
[`clinical_indication`](clinical_indication.md) references, typed by ID prefix via
`DISEASE_NODE_CLASS`. Synonyms matter: "MPNST" and "malignant peripheral nerve sheath
tumor" are the same term, and a consumer matching free text needs both.

The whole file is loaded into memory because the referenced set is not known until
the indications have been scanned; 47,080 terms of labels is small enough for that.

## Quirks

**No ontology hierarchy is emitted.** `parents`, `children`, `ancestors` and
`descendants` are all present and all dropped, so the disease nodes are a flat
vocabulary. `descendants` runs to 25,048 entries on one term — projecting the
hierarchy is a decision with real size consequences, not a free addition.

**`therapeuticAreas` is read and discarded.** The transform loads it into its label
map and never emits it — 26 distinct areas that would be a cheap grouping axis.

**`dbXRefs` and `ancestors` are gated but never read.** They sit in
`required_columns` while the transform reads only `id`, `name`, `exactSynonyms` and
`therapeuticAreas`, so the gate asserts more than the ingest depends on.

**`id` and `code` are never equal.** `code` is the full PURL
(`http://purl.obolibrary.org/obo/GO_0000050`), `id` the short form (`GO_0000050`).
Only `id` is used.

**`ontology` and `synonyms` are structs that restate flat columns**, so the same
synonym data is present twice in two shapes.
