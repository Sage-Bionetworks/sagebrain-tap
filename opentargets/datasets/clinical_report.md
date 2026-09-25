# `clinical_report`

The evidence behind every indication: trials, drug labels and regulatory records. One
file, 74 MB, **289,955 rows, 25 columns** — the largest dataset ingested, and the only
one **partly projected**: the 193,469 `CLINICAL_TRIAL` rows naming a ChEMBL-resolved
drug become trial nodes, the other 96,486 rows do not.

`id` is unique and 100% filled. The scope record for what is projected and why is in
[TRIALS.md](../TRIALS.md); this page describes the source as it stands.

## Four record kinds in one file

`type` governs almost everything else:

| `type` | Rows | `source` | Projected |
|---|---:|---|---|
| `CLINICAL_TRIAL` | 230,990 | AACT (all of them) | 193,469 of them |
| `CURATED_RESOURCE` | 38,125 | TTD, ATC, USAN, INN, WHO, DOI | no |
| `DRUG_LABEL` | 16,029 | DailyMed, FDA | no |
| `REGULATORY_AGENCY` | 4,811 | PMDA, EMA, FDA, Health Canada, MHRA | no |

Only eight fields are populated across all four: `id`, `type`, `source`, `url`,
`title`, `clinicalStage`, `hasExpertReview`, `drugs`. Every `trial*` field is
trial-only — `trialDescription`, `trialOverallStatus` and `trialStudyType` are filled
on exactly 230,990 rows.

**Identifiers are not uniform.** Trial IDs are all `nct<digits>`, 11 characters, no
exceptions. The other three types mix DailyMed UUIDs, sha256 hashes and raw strings
used as keys (`019909s020lbl.pdf`, `a01ab02`, and one row whose ID is a bare FDA URL).

## Columns

Fourteen are in the layout gate — every column the trial transform reads, including
three it reads only to keep a documented claim checked (see below).

| Column | Type | Fill | Gated | Emitted |
|---|---|---:|---|---|
| `id` | large_string | 100%, unique | yes | as the node IRI |
| `type` | large_string | 100%, 4 values | yes | no — the scope filter |
| `clinicalStage` | large_string | 100%, 13 values | yes | `sagebrain:trial_clinical_stage` |
| `drugs` | large_list\<struct\<drugFromSource, drugId\>\> | 100%, max 131 | yes | `sagebrain:trial_drug` (ids only) |
| `diseases` | large_list\<struct\<diseaseFromSource, diseaseId\>\> | 92.4%, max 59 | yes | `biolink:clinical_trial_conditions` (ids only) |
| `trialOfficialTitle` | large_string | 78.4% | yes | `rdfs:label` |
| `trialOverallStatus` | large_string | 79.7% of rows, **100% of trials**, 13 values | yes | `biolink:clinical_trial_overall_status` |
| `trialStartDate` | date32[day] | 78.6% | yes | `sagebrain:trial_start_date`, as `xsd:gYearMonth` |
| `trialWhyStopped` | large_string | 8.8% (25,406) | yes | `sagebrain:trial_stop_reason` |
| `trialStopReasonCategories` | large_list | 25,406 non-empty, 13 values | yes | `sagebrain:trial_stop_reason_category` |
| `qualityControls` | large_list | 104,020 non-empty, 4 values | yes | `sagebrain:trial_quality_control` |
| `source` | large_string | 100%, 23 values | yes | no — `AACT` on every in-scope trial |
| `hasExpertReview` | bool | 100% (55,332 true) | yes | no — false on every in-scope trial |
| `url` | large_string | 100% | yes | no — derivable from `id` |
| `title` | large_string | 100%, 270,022 distinct | no | no — see below |
| `trialDescription` | large_string | 79.7% | no | no |
| `trialStudyType` | large_string | 79.7%, 3 values | no | no |
| `trialPrimaryPurpose` | large_string | 74.3%, 10 values | no | no |
| `trialPhase` | large_string | 67.3%, 7 values | no | no |
| `phaseFromSource` | large_string | 87.6%, 59 values | no | no |
| `trialNumberOfArms` | int32 | 69.4% | no | no |
| `trialLiterature` | large_list | 83,501 non-empty, max 248 | no | no |
| `countries` | large_list | 2,744 non-empty | no | no |
| `sideEffects` | large_list\<struct\> | 2,744 non-empty | no | no |
| `year` | int32 | 0.2% (561) | no | no |

Value counts in this table are **non-null distinct values**. Arrow reports the
null as a distinct entry and counting it inflates every partly-filled column by
one; the fill percentage beside it already says how often the column is empty.

**Three gated columns are read but never emitted.** Over the projected scope
`source` is `AACT` on all 193,469 trials, `hasExpertReview` is false on all of them
(every one of the 55,332 true values is on a non-trial record), and `url` is
`https://clinicaltrials.gov/study/<NCT>` — the node's own identifier in another
form. Reading them anyway, and reporting the counts each run, is what keeps those
three claims checked rather than asserted once in a document.

**`title` is not emitted although it is 100% filled.** Where `trialOfficialTitle` is
present the two are byte-identical on every row; where it is absent — 3,282 in-scope
trials — `title` holds text the release generated (`Report in Phase 3 stage for 2
molecules and Retinitis Pigmentosa`). Those nodes go out unlabelled rather than
carrying a name no registry gave them.

## Vocabularies

`clinicalStage` is the widest in the release — 13 values, and the **only** place
`PHASE_4` (30,509) and `WITHDRAWAL` (866) occur. Both are in `CLINICAL_STAGES` for
that reason. Otherwise: PHASE_2 (64,510), PHASE_1 (50,129), UNKNOWN (39,522),
PHASE_3 (38,958), APPROVAL (29,521). The in-scope trials use nine of the thirteen:
PHASE_2 (50,993), PHASE_3 (33,160), PHASE_1 (32,036), UNKNOWN (29,732), PHASE_4
(28,465), PHASE_1_2 (10,311), PHASE_2_3 (5,148), EARLY_PHASE_1 (3,423) and APPROVAL
(201). `WITHDRAWAL` reaches no slot in the graph.

`type` is gated too, as `REPORT_TYPES`. It is the trial layer's scope filter, and a
renamed or added record kind would change what the layer contains without changing
a line of code.

`trialOverallStatus` has **13** values, not 14 — the fourteenth distinct entry is
the null on every non-trial row. It is filled on all 230,990 trials and on nothing
else, so exactly one status reaches every trial node. The 13 are value-for-value
identical to Biolink's `ClinicalTrialStatusEnum` in the pinned 4.4.4 release,
checked in both directions, which is why the status is the only trial fact here
that rides on a Biolink slot rather than a `sagebrain:` one. In scope: COMPLETED
(113,351), UNKNOWN (23,351), TERMINATED (17,492), RECRUITING (16,426),
ACTIVE_NOT_RECRUITING (7,558), WITHDRAWN (7,032), NOT_YET_RECRUITING (6,238), then
a tail of six under a thousand each.

Note the collision in spelling with `clinicalStage`: `WITHDRAWN` here is a trial
that never enrolled anyone, while `WITHDRAWAL` there is a drug pulled after
approval. Different columns, different subjects, unrelated facts.

`trialStopReasonCategories` has 13 values, is gated as
`TRIAL_STOP_REASON_CATEGORIES`, and pairs perfectly with the free text in both
directions: every row with `trialWhyStopped` has at least one category and no row
has a category without text. A trial can carry up to three. Led by Insufficient_Enrollment
(7,935), Business_Administrative (7,724), Negative (2,412), Study_Design (1,636),
Logistics_Resources (1,587) and Invalid_Reason (1,289); the tail is Another_Study
(1,011), Safety_Sideeffects (926), Study_Staff_Moved (838), Covid19 (804),
Regulatory (602), Uncategorised (528) and No_Context (241).

All such rows are TERMINATED, WITHDRAWN or SUSPENDED — in scope, 15,460, 5,934 and
482, summing to exactly the 21,876 stopped trials. Acceptance check 20 enforces
that rather than trusting it, because a stop reason on a COMPLETED trial would mean
the two columns had come apart and the free text was describing something else.

`qualityControls` has 4 values, gated as `REPORT_QUALITY_CONTROLS`:
INDIRECT_PRIMARY_PURPOSE (54,688), UNVALIDATED_INDICATION (44,496), NO_DISEASE
(21,986), PHASE_IV_NOT_APPROVED (13,665). Only the first and last gate
[`clinical_indication`](clinical_indication.md); all four are emitted, so a consumer
can reproduce that filter or deliberately relax it.

## Joinability

Referential integrity is exact: all 147,845 report IDs referenced by
`clinical_indication` exist here, zero dangling. The reverse is not true — **142,110
reports (49%) are referenced by no indication**: 117,666 trials and 23,505 curated
resources, of which 72,696 carry no QC flag at all.

Internal identifiers resolve only partly: 83.3% of 462,428 drug mentions carry a
ChEMBL ID (12,739 distinct), and 76.2% of 356,950 disease mentions carry an ontology
ID (4,280 distinct). 55,104 rows have no mapped drug and 76,087 no mapped disease.
This is what bounds the trial layer: 37,521 trials are excluded for having no
ChEMBL-resolved drug, and within the 193,469 that are projected a further 16,474
drug and 61,777 disease mentions carry no id and are counted rather than guessed at.
All 9,578 distinct trial drugs are present in `drug_molecule`, so no trial node
dangles on the drug side.

## Quirks

**`trialStartDate` has manufactured day precision.** 57.7% of dates fall on the first
or last day of a month against ~6.6% under a uniform spread, because older records
carry only `YYYY-MM` upstream:

| Start year | Trials | day == 1 | day == last of month |
|---|---:|---:|---:|
| pre-2000 | 3,993 | 1.2% | 92.6% |
| 2000–2009 | 50,877 | 1.4% | 91.7% |
| 2010–2019 | 93,757 | 8.3% | 52.4% |
| 2020+ | 79,210 | 19.2% | 10.5% |

This is why `sagebrain:trial_start_date` is `xsd:gYearMonth`.

**Its range is mostly legitimate.** Dates run 1900-01-31 to 2099-01-01, but 150 of the
156 future starts are 2027–2030 and 121 of those are `NOT_YET_RECRUITING` — real
planned trials. Just six rows fall outside 1950 through ten years past the release:
`1900-01-31`, `1931-06-30`, `2040-01-01`, two at `2050-01-31` and `2099-01-01`, five
of the six on `WITHDRAWN` or `NOT_YET_RECRUITING` trials that never began. The 184
pre-1990 dates are genuine retrospective registrations, including NHLBI trials from
the 1960s. The transform drops exactly the four of those six that are in scope
(1931-06-30, 2040-01-01, 2050-01-31, 2099-01-01) into
`reports/implausible_trial_dates.tsv` without touching a single planned start; the
1900-01-31 row and one of the 2050 rows never reach the check, because neither trial
names a ChEMBL-resolved drug.

**`phaseFromSource` is raw** — 59 variants mixing `PHASE2`, `phase 2`, `APPROVAL` and
`investigative`. Use `trialPhase` (7 values) or `clinicalStage` (13).

**`countries`, `sideEffects` and `year` are effectively empty** — 2,744, 2,744 and 561
rows. `countries` is also dirty, listing `United States` and ` United States` as
separate values among its 128 "distinct" entries.

**`trialLiterature` is PMIDs** — 337,018 values, all digits, 255,869 distinct, with
5,491 nulls inside the lists.
