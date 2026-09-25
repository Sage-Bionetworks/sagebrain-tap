# Open Targets Platform

ChEMBL molecule identity, mechanism-of-action edges to genes, clinical
indications with the stage each reached, and the 193,469 clinical trials behind
them — as RDF, from the Open Targets Platform release files.

Target–disease association *scores* are outside the current drug-layer scope.
A future ranking use case could incorporate these composite scores alongside
the evidence they summarize.

## Quick start

Prerequisites: Python 3.10+, the dependencies below, and network access to the
Open Targets release server and HGNC's Google Cloud Storage archive on the first
run. Gene resolution requires the HGNC complete set; the pipeline downloads its
own pinned copy automatically, so no Reactome download is needed.

Run from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r opentargets/requirements.txt
python -m opentargets.pipeline --release 26.06
```

The pipeline downloads into `opentargets/input/26.06/`, writes Turtle to
`opentargets/26.06/data/rdf/`, the label index to
`opentargets/26.06/data/exports/`, and loads `opentargets/26.06/data/store/`.
The named graph is `urn:sagebrain:opentargets:26.06`.

The HGNC reference goes to `opentargets/input/26.06/hgnc_complete_set.txt`.
Release 26.06 pins the **2026-07-07 quarterly snapshot** by archive URL, byte count,
and SHA-256 in [`manifests/26.06-hgnc.json`](manifests/26.06-hgnc.json).
A custom `--input-dir` also relocates this reference.

Use `--skip-download` to reuse local Parquet datasets and HGNC — both are still
verified against their committed pins — or `--endpoint http://localhost:7011` to
load a server instead of a local store. To prepare the inputs separately:

```bash
python -m opentargets.download_sources --release 26.06
python -m opentargets.pipeline --release 26.06 --skip-download
```

If the pinned HGNC snapshot is already available elsewhere, pass
`--hgnc /path/to/hgnc_complete_set.txt` to either command. This changes the file
location, not the pin: the supplied copy must match the committed checksum and
is never overwritten. The standalone `transform_mechanisms` command also verifies
HGNC and defaults to the copy in its `--indir`. Missing or changed copies fail;
restore the pinned archive rather than substituting HGNC's mutable latest file.

## Pipeline

| Module | Result |
|---|---|
| `download_sources` | Parquet verified against upstream SHA-1 and HGNC verified against its committed SHA-256; source manifest and `release.json` |
| `verify_schemas` | Column and vocabulary gate; committed verification report |
| `transform_molecules` | `molecules.ttl` — ChEMBL nodes, preferred names, synonyms, trade names, structures |
| `transform_mechanisms` | `mechanisms.ttl` — compound→gene edges and HGNC gene nodes; unresolved ids in `reports/` |
| `transform_indications` | `indications.ttl` — compound→disease edges with stage, and disease/phenotype nodes |
| `transform_trials` | `trials.ttl` — clinical trial nodes with stage, status, start month, stop reasons and quality flags; trial-only disease nodes; dropped dates in `reports/` |
| `export_label_index` | `exports/chembl_labels.tsv` — label→ChEMBL id, for consumers resolving free text |
| `load_graph` | Release graph and `void.ttl` metadata in the default graph |
| `acceptance_checks` | Twenty checks: node typing, edge resolution, vocabularies, gene keying, size, known facts, stage-slot separation, trial keying/links/dates/status, model terms |

Every module supports `python -m opentargets.<module> --help`.
See [design decisions](DESIGN.md), [source dataset notes](datasets/) and
[manifests](manifests/).

## What pins a release

Open Targets publishes `release_data_integrity` — a SHA-1 for every file in the
release — with its own `.sha1` alongside. The ingest verifies that manifest, then
verifies every download against it, and records the manifest's own digest as the
release anchor. That anchor is in `release.json` and in the graph's VoID as
`sagebrain:source_integrity_digest`.

`manifests/26.06-sources.tsv` is committed and carries, per file, upstream's SHA-1,
our SHA-256, the byte count and the dataset's row count. Only the manifest is
committed, never the data.

```bash
python -m opentargets.download_sources --release 26.06 --verify   # no network
```

Datasets are directories and the transforms read the whole directory, so the
check runs both ways: every pinned part must be present and unchanged, and a
`.parquet` file the manifest does not list is reported as `UNPINNED` and fails
the run. Delete stale parts rather than keeping them — left in place, an
obsolete part from a previous release is ingested and counted.

HGNC is an independent input and is not covered by Open Targets' integrity
manifest. Its separate pin is checked on download, offline verification, and
mechanism transformation, and recorded in `release.json`. Quarterly snapshots
are used because [HGNC retains them while monthly archives expire after a year](https://www.genenames.org/download/archive/).
For a new Open Targets release, add a reviewed `<release>-hgnc.json` alongside its
source manifest (or in `--manifest-dir`), using the same fields as the existing
pin. Normal pipeline runs never create or replace HGNC pins.

A release changing under a stable path means the contents changed. `--verify`
reports drift and never edits the pin.

## Reading the graph correctly

Five things the triples cannot tell you, all also recorded in VoID:

- **`sagebrain:max_clinical_stage` belongs to an edge, not a drug.** It is the maximum
  over the reports behind one drug–disease pair. A drug that failed phase 3 for one
  disease and was approved for another carries both, on different edges. Never quote
  a stage without its indication. `sagebrain:overall_clinical_stage` is a different
  fact, not a duplicate: it is the compound's own highest stage — not ChEMBL's
  numeric `max_phase`, and **not**
  derivable from the edges — 875 compounds carry a higher value than their edges imply
  and 1,276 carry a stage with no indication edge at all, 391 of them at `APPROVAL`.
  It says nothing about *which* disease.
- **Indications use `biolink:treats_or_applied_or_studied_to_treat`, not
  `biolink:treats`.** 75,293 of 86,468 rows are below APPROVAL. A phase-1 trial is a
  compound being studied for a disease, not one that treats it.
- **`sagebrain:target_type` decides what a mechanism edge claims.** `single protein`
  is a drug–target pair. `protein family`, `protein complex` and `selectivity group`
  name a *group*, and the edges enumerate its members — trametinib's "MEK1/2
  inhibitor" row becomes one edge to MAP2K1 and one to MAP2K2 from a single claim.
  At 26.06 group edges outnumber single-protein edges 9,506 to 5,202, so counting
  drug–target pairs without filtering over-counts by roughly two thirds.

- **A missing trial node is an identity limit, not evidence of no trial.** Only
  `type = CLINICAL_TRIAL` records naming a ChEMBL-resolved drug become nodes:
  193,469 of the release's 230,990 trials. The 37,521 excluded ones *do* name a
  drug — they just have no ChEMBL id for it — and they concentrate in cell,
  tissue, microbiota and blood-product therapies (mesenchymal stem cells, CAR-T,
  platelet-rich plasma, faecal microbiota transplant, convalescent plasma). A
  further 50,971 in-scope trials map no disease at all and are drug-only nodes.
- **`sagebrain:trial_start_date` is a month, and says so.** It is typed
  `xsd:gYearMonth` because the source's day is manufactured for older records:
  92.6% of pre-2000 start dates fall on the last day of a month, against 10.5%
  for 2020 and later. Four placeholder dates (1931, and three beyond 2040) are
  dropped and listed in `reports/implausible_trial_dates.tsv`; those trials keep
  their node.

Genes are HGNC-keyed, so they join Reactome's gene nodes by IRI with no mapping
table; the Ensembl id Open Targets used is kept on each edge as
`biolink:original_object`.

There are **three** clinical-stage slots, narrowest last:
`sagebrain:overall_clinical_stage` on a molecule,
`sagebrain:max_clinical_stage` on a drug–disease edge, and
`sagebrain:trial_clinical_stage` on one trial. Acceptance check 10 fails if any
subject carries more than one. 28,465 trials are `PHASE_4`, a value no other slot
in the graph carries, because the source collapses phase 4 into `APPROVAL` at the
compound level.

## The label index

`exports/chembl_labels.tsv` is the general half of resolving free-text compound
names. One row per (case-folded label, molecule), so one row is one candidate:

```
label	label_folded	kind	source	chembl_id	molecule_name	drug_type	ambiguous
AZD-6244	azd-6244	synonym	ChEMBL	CHEMBL1614701	SELUMETINIB	Small molecule	no
```

101,195 rows over 22,407 molecules, 97,835 distinct folded labels of which 2,607 are
ambiguous. Ambiguity is recorded, not resolved: `(+)-epicatechin` is one molecule's
preferred name and its enantiomer's synonym, and picking one silently would turn an
unresolvable string into a confident wrong answer.

Normalisation is case folding only. Stronger rules — stripping doses, plate codes,
salt prefixes, stereochemistry, splitting `A + B` combinations — trade recall for
precision differently per source, so they belong with the consumer that knows its own
field conventions.

## Recorded 26.06 run

| | |
|---|---|
| Release | 26.06, published 2026-06-23, CC0-1.0 |
| Provenance anchor | `sha1(release_data_integrity)` = `e49fc309…` |
| Molecules | 22,407 (18,697 with InChIKey and SMILES; 1,999 with a parent) |
| Mechanism edges | 14,708 over 1,548 HGNC gene nodes (689 duplicate rows collapsed) |
| Ensembl→HGNC | 15,397 of 15,404 lookups resolved; 2 distinct unresolved ids |
| Indication edges | 86,468 — 11,364 drugs × 3,749 diseases, 11,175 at APPROVAL |
| Trials | 193,469 of 230,990 — 337,760 drug and 177,344 condition links; 21,876 with a stop reason, all `TERMINATED`/`WITHDRAWN`/`SUSPENDED` |
| Disease/phenotype nodes | 4,059 — 3,749 from indications, 310 only a trial reaches |
| Release graph | 2,645,417 triples |

See [`manifests/26.06-acceptance.md`](manifests/26.06-acceptance.md). Nineteen
structural checks pass; the model-term review warns that 17 `sagebrain:` terms are
not yet defined in sagebrain-model, which is the intended to-do list rather than a
failure.

## A trap worth knowing

The release's own `manifest.json` reports `"result": "failure"` for 26.06. The single
failed step is `pos_tarballs` — packaging the release into tarballs — and every step
producing the datasets used here succeeded. The flag is recorded in `release.json`
rather than gating the ingest, and it is not evidence the data is bad.

## Development

```bash
python -m unittest discover -s opentargets/tests -v
```
