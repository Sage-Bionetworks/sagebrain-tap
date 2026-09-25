# Open Targets clinical trial layer

[transform_trials.py](transform_trials.py) projects clinical trial records from
`clinical_report` into `trials.ttl`. This document describes the projection's scope and
interpretation. See [DESIGN.md](DESIGN.md) for the overall model, [README.md](README.md)
for operations, and the [source dataset notes](datasets/clinical_report.md) for release
measurements.

## Scope

The projection includes `CLINICAL_TRIAL` records naming at least one ChEMBL-resolved
drug. Trial identifiers are normalized to ClinicalTrials.gov accessions. Other report
types use heterogeneous identifiers for which this ingest has no stable mapping across
releases.

Trials without a ChEMBL-resolved drug are excluded. These records include cell, tissue,
microbiota, and blood-product therapies, as well as broad intervention names such as
`antibiotics`. Coverage therefore depends on source identifier resolution. An absent
trial node does not establish that no trial exists.

Mapped drug mentions become compound links; unmapped mentions are counted but do not
produce links. Acceptance checks verify that trial links resolve to typed nodes. Trials
without mapped diseases remain as nodes with drug links.

Disease and phenotype nodes cover terms referenced by either indications or trials.
`indications.ttl` emits indication-referenced terms, and `trials.ttl` emits the
additional terms referenced only by trials.

## Clinical history

Trial nodes retain clinical stage, overall status, stop reasons, and quality-control
flags. These properties preserve details that an indication's maximum clinical stage
cannot express, such as whether a trial stopped early.

Interpret stop reasons with the overall status: `TERMINATED` denotes an early end,
`WITHDRAWN` a trial stopped before enrollment, and `SUSPENDED` a temporary halt.
Acceptance checks validate this relationship.

All source quality-control flags are emitted. Open Targets excludes reports flagged
`PHASE_IV_NOT_APPROVED` or `INDIRECT_PRIMARY_PURPOSE` when constructing
`clinical_indication`; the trial projection does not apply that filter. Consumers can
use the retained flags to select the scope appropriate to their analysis.

Report types, stop-reason categories, quality-control flags, and overall statuses are
validated against the vocabularies in [common.py](common.py). Validation scans the
source dataset, including records outside the projected scope.

## Start dates

Start dates are emitted as `xsd:gYearMonth`. Older source records may contain only a
year and month, with a day supplied during normalization. Month precision avoids
implying that this day was recorded by the registry. The [source date
analysis](datasets/clinical_report.md#data-characteristics) documents the observed
distribution.

The transform accepts years from 1950 through ten years after the release year. Dates
outside this window are omitted and listed in `reports/implausible_trial_dates.tsv`,
while their trial nodes are retained. A run fails if more than 1% of dated in-scope
trials fall outside the window. Planned future starts within the window are retained.

## RDF representation

A trial is a study node with links to compounds and conditions. Mechanisms and
indications use reified associations because they represent claims connecting entities.

```turtle
CLINICALTRIALS:NCT01160926
    a biolink:ClinicalTrial ;
    rdfs:label "Dual Phase I Studies to Determine the Dose of Cediranib (AZD2171) or AZD6244 to Use With Conventional Rectal Chemoradiotherapy" ;
    sagebrain:trial_clinical_stage "PHASE_1" ;
    biolink:clinical_trial_overall_status "TERMINATED" ;
    sagebrain:trial_start_date "2010-07"^^xsd:gYearMonth ;
    sagebrain:trial_stop_reason "2 DLTs had been reported from first 4 patients on lowest possible dose cohort." ;
    sagebrain:trial_stop_reason_category "Negative" , "Safety_Sideeffects" ;
    sagebrain:trial_quality_control "UNVALIDATED_INDICATION" ;
    sagebrain:trial_drug CHEMBL:CHEMBL1614701 , CHEMBL:CHEMBL491473 ;
    biolink:clinical_trial_conditions obo:MONDO_0006519 .
```

| Fact | Property | Mapping rationale |
|---|---|---|
| Condition studied | `biolink:clinical_trial_conditions` | Domain and range match trial and condition nodes |
| Overall status | `biolink:clinical_trial_overall_status` | The source vocabulary matches Biolink's status enum |
| Compound studied | `sagebrain:trial_drug` | Links to Compound nodes; Biolink's intervention slot has a different range |
| Clinical stage | `sagebrain:trial_clinical_stage` | Preserves the Open Targets vocabulary, which differs from Biolink's research phase enum |
| Start month | `sagebrain:trial_start_date` | Explicit month-precision representation, validated as `xsd:gYearMonth` |

Clinical-stage properties have distinct subjects: `overall_clinical_stage` on a
molecule, `max_clinical_stage` on a drug–disease association, and `trial_clinical_stage`
on a trial. Acceptance checks enforce this separation. Trial nodes retain `PHASE_4`,
which the source represents as `APPROVAL` at the molecule level.

## Fields outside the projection

The transform reads `source`, `hasExpertReview`, and `url` to report deviations from the
observed source characteristics. In the pinned release, projected trials come from AACT,
have no expert-review flag, and carry a registry URL that can be derived from their
accession. These fields are not emitted.

Labels use `trialOfficialTitle`. When the registry title is missing, the source's
`title` field may contain generated text; this text is not used as a substitute, and the
trial remains unlabeled.

`countries`, `sideEffects`, `year`, and `phaseFromSource` are outside the current
projection. Their coverage and formatting are documented in the source notes.
`trialLiterature` is also unused; adding publication links remains an extension under
consideration.

Indication `clinicalReportIds` are counted but not emitted as links. Joining trial nodes
to indications by drug and disease does not reproduce the supporting report list
exactly: indications aggregate multiple report types and apply a different
quality-control filter.

## Recorded 26.06 result

| | |
|---|---:|
| Report rows read | 289,955 |
| Trials | 230,990 |
| In scope | 193,469 |
| Dropped — no ChEMBL-resolved drug | 37,521 |
| Trial–drug links | 337,760 over 9,578 drugs |
| Trial–condition links | 177,344 over 3,841 terms |
| Drug-only trials | 50,971 |
| Disease nodes only a trial references | 310 |
| Start dates emitted | 190,705 |
| Start dates dropped as placeholders | 4 |
| Trials with a stop reason | 21,876, carrying 23,786 categories |
| Statuses | 13, all filled — COMPLETED 113,351, UNKNOWN 23,351, TERMINATED 17,492 |
| Quality-control flags | 115,732 over 88,815 trials |
| Trials with no registry title | 3,282 |
| Triples | 1,639,457 |

Unmapped mentions within in-scope trials: 16,474 drug and 61,777 disease. These mentions
are counted but do not produce entity links.
