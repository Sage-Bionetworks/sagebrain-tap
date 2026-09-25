# Open Targets Platform

This pipeline converts Open Targets Platform release files to RDF: ChEMBL compounds,
gene targets, clinical indications, and clinical trials. It preserves source
identifiers, clinical stages, and provenance for downstream analysis.

Target–disease association *scores* are outside the current drug-layer scope. A future
ranking use case could incorporate these composite scores alongside the evidence they
summarize.

## Quick start

Prerequisites: Python 3.10+, the dependencies below, and network access to the Open
Targets release server and HGNC's Google Cloud Storage archive on the first run. Gene
resolution requires the HGNC complete set; the pipeline downloads its own pinned copy
automatically, so no Reactome download is needed.

Run from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r opentargets/requirements.txt
python -m opentargets.pipeline --release 26.06
```

The pipeline downloads into `opentargets/input/26.06/`, writes Turtle to
`opentargets/26.06/data/rdf/`, the label index to `opentargets/26.06/data/exports/`, and
loads `opentargets/26.06/data/store/`. The named graph is
`urn:sagebrain:opentargets:26.06`.

The HGNC reference goes to `opentargets/input/26.06/hgnc_complete_set.txt`. Release
26.06 pins the **2026-07-07 quarterly snapshot** by archive URL, byte count, and SHA-256
in [`manifests/26.06-hgnc.json`](manifests/26.06-hgnc.json). A custom `--input-dir` also
relocates this reference.

Use `--skip-download` to reuse local Parquet datasets and HGNC — both are still verified
against their committed pins — or `--endpoint http://localhost:7011` to load a server
instead of a local store. To prepare the inputs separately:

```bash
python -m opentargets.download_sources --release 26.06
python -m opentargets.pipeline --release 26.06 --skip-download
```

If the pinned HGNC snapshot is already available elsewhere, pass `--hgnc
/path/to/hgnc_complete_set.txt` to either command. This changes the file location, not
the pin: the supplied copy must match the committed checksum and is never overwritten.
The standalone `transform_mechanisms` command also verifies HGNC and defaults to the
copy in its `--indir`. Missing or changed copies fail; restore the pinned archive rather
than substituting HGNC's mutable latest file.

## Pipeline

| Module | Result |
|---|---|
| `download_sources` | Parquet verified against upstream SHA-1 and HGNC verified against its committed SHA-256; source manifest and `release.json` |
| `verify_schemas` | Required-column and vocabulary validation; verification report |
| `transform_molecules` | `molecules.ttl` — ChEMBL nodes, preferred names, synonyms, trade names, structures |
| `transform_mechanisms` | `mechanisms.ttl` — compound→gene edges and HGNC gene nodes; unresolved ids in `reports/` |
| `transform_indications` | `indications.ttl` — compound→disease edges with stage, and disease/phenotype nodes |
| `transform_trials` | `trials.ttl` — clinical trial nodes with stage, status, start month, stop reasons and quality flags; trial-only disease nodes; dropped dates in `reports/` |
| `export_label_index` | `exports/chembl_labels.tsv` — label→ChEMBL id, for consumers resolving free text |
| `load_graph` | Release graph and `void.ttl` metadata in the default graph |
| `acceptance_checks` | Structural validation, reference resolution, known facts, clinical-stage scope, trial properties, and vocabulary definitions |

Every module supports `python -m opentargets.<module> --help`. See [design
decisions](DESIGN.md), [source dataset notes](datasets/) and [manifests](manifests/).

## Release integrity

Open Targets publishes `release_data_integrity` — a SHA-1 for every file in the release
— with its own `.sha1` alongside. The ingest verifies that manifest, then verifies every
download against it, and records the manifest's own digest as the release anchor. That
anchor is in `release.json` and in the graph's VoID as
`sagebrain:source_integrity_digest`.

`manifests/26.06-sources.tsv` is committed and carries, per file, upstream's SHA-1,
local SHA-256, the byte count and the dataset's row count. Only the manifest is
committed, never the data.

```bash
python -m opentargets.download_sources --release 26.06 --verify   # no network
```

Transforms read all Parquet files in each dataset directory. Verification requires every
pinned file to be present and unchanged, and rejects unlisted files as `UNPINNED`.
Remove stale parts before rerunning the pipeline.

HGNC is an independent input and is not covered by Open Targets' integrity manifest. Its
separate pin is checked on download, offline verification, and mechanism transformation,
and recorded in `release.json`. Quarterly snapshots are used because [HGNC retains them
while monthly archives expire after a
year](https://www.genenames.org/download/archive/). For a new Open Targets release, add
a reviewed `<release>-hgnc.json` alongside its source manifest (or in `--manifest-dir`),
using the same fields as the existing pin. Normal pipeline runs never create or replace
HGNC pins.

`--verify` reports changes to pinned inputs without modifying the pins.

## Interpreting the graph

Clinical-stage properties have distinct scopes:

| Property | Scope | Interpretation |
|---|---|---|
| `sagebrain:overall_clinical_stage` | Molecule | Highest stage supplied by Open Targets; does not identify an indication |
| `sagebrain:max_clinical_stage` | Drug–disease association | Maximum stage across the source's supporting clinical reports |
| `sagebrain:trial_clinical_stage` | Trial | Stage recorded for the individual trial |

The molecule-level stage is copied from the source and cannot be derived reliably from
the indication associations. It uses the Open Targets vocabulary, distinct from ChEMBL's
numeric `max_phase`. Acceptance checks keep the stage properties on their respective
subjects. Trial nodes retain `PHASE_4`; the source represents this stage as `APPROVAL`
at the molecule level.

Other interpretation requirements are documented in VoID metadata:

- **Indications include investigational uses.** The predicate
  `biolink:treats_or_applied_or_studied_to_treat` covers both studied and approved
  indications. An indication association alone does not establish efficacy.
- **Target type determines mechanism scope.** For a family, complex, or
  selectivity group, associations enumerate members of a shared target claim.
  They do not establish independently measured interactions with each gene.
  Filter on `sagebrain:target_type` when counting individual drug–target pairs.
- **Trial coverage depends on identifier resolution.** Only `CLINICAL_TRIAL`
  records naming a ChEMBL-resolved drug become nodes. Excluded records include
  cell and tissue therapies and other interventions without a ChEMBL identifier.
  Trials with mapped drugs but no mapped disease remain in the graph.
- **Start dates use month precision.** `sagebrain:trial_start_date` is typed as
  `xsd:gYearMonth` because older source records may have a day supplied during
  normalization. Dates outside the configured plausibility window are reported
  in `reports/implausible_trial_dates.tsv`; the trial nodes are retained.

Genes use HGNC identifiers and join Reactome genes directly by IRI. The source Ensembl
identifier is preserved on each mechanism association as `biolink:original_object`.

## Label index

`exports/chembl_labels.tsv` supports free-text compound name resolution. Each row
represents a candidate molecule for a case-folded label:

```tsv
label	label_folded	kind	source	chembl_id	molecule_name	drug_type	ambiguous
AZD-6244	azd-6244	synonym	ChEMBL	CHEMBL1614701	SELUMETINIB	Small molecule	no
```

A label may identify multiple molecules. The export records this ambiguity for consumers
to resolve using their own context. For example, `(+)-epicatechin` appears as a
preferred name for one molecule and a synonym for its enantiomer. Labels containing
control characters are excluded and listed in `reports/rejected_labels.tsv`.

Normalization is limited to case folding. Dose removal, salt handling, stereochemistry,
and combination splitting depend on the source field and remain consumer
responsibilities.

## Deposit to SageBrain

The local loader uses a release graph. The shared deposit command uses a dated snapshot
graph, `urn:sagebrain:opentargets:<YYYY-MM-DD>`.

Before depositing, prepare `opentargets/<release>/data/manifest.ttl` with provenance for
that destination graph, retaining the upstream release in `pav:version`. Keep the
manifest outside `data/rdf/`, and ensure any VoID metadata included in the payload
identifies the same destination. Manifest preparation is currently a separate step from
the pipeline.

For a prepared release and snapshot:

```bash
python -m pip install -r shared/requirements.txt
python -m shared.deposit --portal opentargets --snapshot YYYY-MM-DD \
  --data-dir opentargets/26.06/data/rdf \
  --manifest opentargets/26.06/data/manifest.ttl --dry-run
```

Replace `YYYY-MM-DD` with the snapshot date. After reviewing the plan, replace
`--dry-run` with `--watch` to upload and monitor ingestion. Deposit uploads the manifest
last to trigger the load. Its destination checks reject an occupied snapshot prefix by
default.

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
| Release graph | 2,645,418 triples |
| Label index | 101,195 rows; 97,835 distinct folded labels, including 2,607 ambiguous labels |

See the [acceptance report](manifests/26.06-acceptance.md) for validation results and
the local terms awaiting definition in sagebrain-model. Undefined local terms are
reported as warnings; undefined Biolink terms fail validation.

## Upstream build status

The release's `manifest.json` reports `"result": "failure"` for release 26.06. The
failed step, `pos_tarballs`, packages the release into tarballs; the steps producing the
datasets used here succeeded. The pipeline records this status in `release.json` without
treating it as an input validation failure.

## Development

```bash
python -m unittest discover -s opentargets/tests -v
```
