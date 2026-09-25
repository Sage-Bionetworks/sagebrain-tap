# Open Targets clinical trial layer

`clinical_report` is projected as trial nodes by
[transform_trials.py](transform_trials.py), emitted to `trials.ttl`. This page is
the scope record: what the layer contains, what it deliberately leaves out, and
why each boundary sits where it does.

See [DESIGN.md](DESIGN.md) for the drug-layer model this extends,
[README.md](README.md) for operations, [datasets/clinical_report.md](datasets/clinical_report.md)
for the source as it stands, and [manifests/](manifests/) for the pinned release
layout.

## Scope

**Only `type = CLINICAL_TRIAL`.** That is 230,990 of the 289,955 records. The other
three types — curated resources, drug labels and regulatory records — are provenance
about a drug rather than trial evidence, and they have no usable identity: trial IDs
are uniformly `nct<digits>`, with no exceptions, while the rest are a mix of DailyMed
UUIDs, sha256 hashes and raw strings such as `019909s020lbl.pdf` or a bare URL. Only
trials get a CURIE that survives a release.

**Only trials that join an emitted molecule.** 193,469 trials (83.8%) have at
least one ChEMBL-resolved drug; their 9,578 distinct drugs are all already in
`drug_molecule`, so no trial dangles. The 37,521 dropped trials do all name a drug —
what they lack is a ChEMBL ID for it, and they concentrate in cell, tissue, microbiota
and blood-product therapies (mesenchymal stem cells, CAR-T, platelet-rich plasma,
faecal microbiota transplant, convalescent plasma) plus class-level names like
`antibiotics` or `statin`. This filter is therefore an identity limit inherited from
ChEMBL, not a judgement about intervention type, and it removes the modalities that
[DESIGN.md](DESIGN.md) §4 already flags as a loose fit for `ChemicalEntity`.

**Start dates as `xsd:gYearMonth`.** `trialStartDate` is a date32, but its day is
manufactured for older records: 92.6% of pre-2000 and 91.7% of 2000–2009 start dates
fall on the last day of a month, against 10.5% for 2020 and later, because the source
value is `YYYY-MM` and normalisation supplies the day. Across all trials 57.7% land on
the first or last of a month, where a uniform spread would give about 6.6%. Truncating
to year-month asserts only what the release actually carries.

Range is a separate check from precision, and future dates are mostly real: 114
in-scope trials start in 2027–2030, 92 of them `NOT_YET_RECRUITING` (150 and 121
across all trials). Only a handful are placeholders. Dates outside 1950 through ten
years past the release are dropped, which at 26.06 isolates exactly four in-scope
rows — `1931-06-30`, `2040-01-01`, `2050-01-31` and `2099-01-01`, three of the four
on `WITHDRAWN` trials that never began — without discarding a single planned start.
They go to `reports/implausible_trial_dates.tsv` and the trial node is still emitted.
Two further out-of-window rows exist in the release and never reach the check, because
the drug filter already removed them: a 1900-01-31 `WITHDRAWN` trial and a second
2050-01-31. The 169 in-scope pre-1990 dates (184 across all trials) are genuine
retrospective registrations, including NHLBI trials from the 1960s. Above 1% of dated
trials the run fails instead, because at that rate the date scheme changed rather than
a few rows being wrong.

**Diseases.** Trials reference 3,841 terms, 310 of which no indication references
(184 MONDO, 59 HP, 56 EFO, 4 OBA, 4 GO, 3 Orphanet). Those nodes are emitted too,
which widens the disease pass from "terms referenced by indications" to terms
referenced by either — 4,059 in all. `indications.ttl` emits the terms an indication
references and `trials.ttl` emits exactly the remainder, so every term is asserted
once. A further 50,971 in-scope trials map no disease at all and become drug-only
trial nodes.

## What this adds

80,145 trials are new to the graph; the other 113,324 are already referenced by
indication associations, which previously carried only
`sagebrain:clinical_report_count`. 21,876 carry a stop reason, and every record
with `trialWhyStopped` free text also carries a category from a 13-value vocabulary
(`Insufficient_Enrollment`, `Business_Administrative`, `Negative`,
`Safety_Sideeffects` and others) — the trial history that
[DESIGN.md](DESIGN.md) §4 notes an indication's maximum stage cannot preserve.

Every trial also carries `trialOverallStatus`, which is what makes a stop reason
mean something: all 21,876 sit on `TERMINATED` (15,460), `WITHDRAWN` (5,934) or
`SUSPENDED` (482), and a `Negative` category on a trial that halted midway is not
the same claim as one on a trial that never enrolled a participant. That pairing
is checked rather than assumed (acceptance check 20).
`qualityControls` has four values, of which only `PHASE_IV_NOT_APPROVED` and
`INDIRECT_PRIMARY_PURPOSE` gate `clinical_indication`; emitting all four lets a
consumer reproduce or relax that filter.

The three new vocabularies are constants in [common.py](common.py)
(`TRIAL_STOP_REASON_CATEGORIES`, `REPORT_QUALITY_CONTROLS`,
`TRIAL_OVERALL_STATUSES`) and are gated in [verify_schemas.py](verify_schemas.py)
like the existing ones, across all 289,955 rows rather than only the trials in
scope. `REPORT_TYPES` gates the scope filter itself.

## What a trial node looks like

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

A **node**, not a reified association, unlike mechanisms and indications. Those are
associations because each *is* an assertion with a subject and an object; a trial is
a study that happened, and the drugs and diseases are its properties. The
alternative shape — one association per (trial, drug, disease) — would also have
cost roughly 3M triples for the cross product, but the modelling is the reason and
the arithmetic merely agrees.

Biolink is used wherever it fits and a local slot only where it does not, which
here splits four ways:

| Fact | Slot | Why |
|---|---|---|
| Condition studied | `biolink:clinical_trial_conditions` | Domain and range both fit exactly |
| Overall status | `biolink:clinical_trial_overall_status` | `ClinicalTrialStatusEnum` matches Open Targets' 13 values value-for-value |
| Drug tested | `sagebrain:trial_drug` | Biolink's `clinical_trial_interventions` has range `clinical intervention`; these are `biolink:ChemicalEntity`, and this ingest does not even type them `biolink:Drug` ([schema/opentargets.yaml](../schema/opentargets.yaml), `Compound`) |
| Stage | `sagebrain:trial_clinical_stage` | Biolink's `clinical_trial_phase` has range `ResearchPhaseEnum`, a different vocabulary that cannot express `APPROVAL` or `UNKNOWN` |
| Start month | `sagebrain:trial_start_date` | Biolink's `clinical_trial_start_date` has range `string`, which cannot carry the month-precision claim |

Emitting a Biolink slot whose declared range the objects do not satisfy would be a
range lie, so the last three are local and say why.

`sagebrain:trial_clinical_stage` is a **third** stage slot, and the narrowest:
`max_clinical_stage` scopes to a drug–disease pair, `overall_clinical_stage` to a
molecule, and this to one trial. Acceptance check 11 fails if any subject carries
two of them. 28,465 trials are `PHASE_4`, a value no other slot in the graph
carries, because the source collapses phase 4 into `APPROVAL` at the compound level.

## Fields left out

`countries` and `sideEffects` are filled on 2,744 rows each and `year` on 561;
`countries` is also dirty, listing `United States` and ` United States` as separate
values. `phaseFromSource` holds 59 unnormalised variants mixing `PHASE2`, `phase 2`
and `investigative` — read `trialPhase` or `clinicalStage` instead.

`source`, `hasExpertReview` and `url` are gated and **read** but not emitted, because
over the projected scope they are constant or derivable: `source` is `AACT` on all
193,469 trials, `hasExpertReview` is false on all of them, and `url` is
`https://clinicaltrials.gov/study/<NCT>`, which is the node's own identifier. Reading
them anyway is what keeps those three claims checked against each release rather
than asserted once here.

`title` is not emitted either. Where `trialOfficialTitle` is present the two columns
are byte-identical; where it is absent — 3,282 in-scope trials — `title` holds
generated text (`Report in Phase 3 stage for 2 molecules and Retinitis Pigmentosa`),
which is derived, not a name the registry gave the trial. Those nodes go out
unlabelled.

`trialLiterature` (337,018 PMIDs over 83,501 rows, 255,869 distinct) is read by
nothing and is therefore absent from both the layout gate and the schema, as
`crossReferences` is on the molecule side. It is the obvious next addition: it
would let a trial node cite the publications reporting it.

`clinicalReportIds` on `clinical_indication` is still not emitted, so the graph does
not state which trials an indication edge was computed from. A consumer joins the two
sides on (drug, disease), which is nearly but not exactly the same set: the
indication's report list spans all four record kinds and is filtered on two
quality-control flags, while trial nodes are trials only and unfiltered.

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

Unmapped mentions *inside* in-scope trials: 16,474 drug and 61,777 disease. Those
are gaps in the source's own resolution, counted rather than guessed at.
