# `clinical_report`

The evidence behind every indication: trials, drug labels and regulatory records. One
file, 74 MB, **289,955 rows, 25 columns** — the largest dataset ingested and the only
one **pinned and column-gated but not projected**.

`id` is unique and 100% filled. The decided scope for projecting it is in
[TRIALS.md](../TRIALS.md); this page describes the source as it stands.

## Four record kinds in one file

`type` governs almost everything else:

| `type` | Rows | `source` |
|---|---:|---|
| `CLINICAL_TRIAL` | 230,990 | AACT (all of them) |
| `CURATED_RESOURCE` | 38,125 | TTD, ATC, USAN, INN, WHO, DOI |
| `DRUG_LABEL` | 16,029 | DailyMed, FDA |
| `REGULATORY_AGENCY` | 4,811 | PMDA, EMA, FDA, Health Canada, MHRA |

Only eight fields are populated across all four: `id`, `type`, `source`, `url`,
`title`, `clinicalStage`, `hasExpertReview`, `drugs`. Every `trial*` field is
trial-only — `trialDescription`, `trialOverallStatus` and `trialStudyType` are filled
on exactly 230,990 rows.

**Identifiers are not uniform.** Trial IDs are all `nct<digits>`, 11 characters, no
exceptions. The other three types mix DailyMed UUIDs, sha256 hashes and raw strings
used as keys (`019909s020lbl.pdf`, `a01ab02`, and one row whose ID is a bare FDA URL).

## Columns

Eleven are in the layout gate even though nothing is projected, so a layout change
fails at the start of a run rather than the day the trial layer lands.

| Column | Type | Fill | Gated |
|---|---|---:|---|
| `id` | large_string | 100%, unique | yes |
| `type` | large_string | 100%, 4 values | yes |
| `source` | large_string | 100%, 23 values | yes |
| `clinicalStage` | large_string | 100%, 13 values | yes |
| `url` | large_string | 100% | yes |
| `hasExpertReview` | bool | 100% (55,332 true) | yes |
| `drugs` | large_list\<struct\<drugFromSource, drugId\>\> | 100%, max 131 | yes |
| `diseases` | large_list\<struct\<diseaseFromSource, diseaseId\>\> | 92.4%, max 59 | yes |
| `trialWhyStopped` | large_string | 8.8% (25,406) | yes |
| `trialStopReasonCategories` | large_list | 25,406 non-empty, 13 values | yes |
| `trialStartDate` | date32[day] | 78.6% | yes |
| `title` | large_string | 100%, 270,022 distinct | no |
| `trialDescription` | large_string | 79.7% | no |
| `trialOfficialTitle` | large_string | 78.4% | no |
| `trialOverallStatus` | large_string | 79.7%, 14 values | no |
| `trialStudyType` | large_string | 79.7%, 4 values | no |
| `trialPrimaryPurpose` | large_string | 74.3%, 11 values | no |
| `trialPhase` | large_string | 67.3%, 8 values | no |
| `phaseFromSource` | large_string | 87.6%, 60 values | no |
| `trialNumberOfArms` | int32 | 69.4% | no |
| `trialLiterature` | large_list | 83,501 non-empty, max 248 | no |
| `qualityControls` | large_list | 104,020 non-empty, 4 values | no |
| `countries` | large_list | 2,744 non-empty | no |
| `sideEffects` | large_list\<struct\> | 2,744 non-empty | no |
| `year` | int32 | 0.2% (561) | no |

## Vocabularies

`clinicalStage` is the widest in the release — 13 values, and the **only** place
`PHASE_4` (30,509) and `WITHDRAWAL` (866) occur. Both are in `CLINICAL_STAGES` for
that reason. Otherwise: PHASE_2 (64,510), PHASE_1 (50,129), UNKNOWN (39,522),
PHASE_3 (38,958), APPROVAL (29,521).

`trialStopReasonCategories` has 13 values and pairs perfectly with the free text:
every row with `trialWhyStopped` also has a category. Led by Insufficient_Enrollment
(7,935), Business_Administrative (7,724), Negative (2,412), Study_Design (1,636),
Safety_Sideeffects (926), Covid19 (804). All such rows are TERMINATED, WITHDRAWN or
SUSPENDED.

`qualityControls` has 4 values: INDIRECT_PRIMARY_PURPOSE (54,688),
UNVALIDATED_INDICATION (44,496), NO_DISEASE (21,986), PHASE_IV_NOT_APPROVED (13,665).
Only the first and last gate [`clinical_indication`](clinical_indication.md).

## Joinability

Referential integrity is exact: all 147,845 report IDs referenced by
`clinical_indication` exist here, zero dangling. The reverse is not true — **142,110
reports (49%) are referenced by no indication**: 117,666 trials and 23,505 curated
resources, of which 72,696 carry no QC flag at all.

Internal identifiers resolve only partly: 83.3% of 462,428 drug mentions carry a
ChEMBL ID (12,739 distinct), and 76.2% of 356,950 disease mentions carry an ontology
ID (4,280 distinct). 55,104 rows have no mapped drug and 76,087 no mapped disease.

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

**Its range is mostly legitimate.** Dates run 1900-01-31 to 2099-01-01, but 150 of the
156 future starts are 2027–2030 and 121 of those are `NOT_YET_RECRUITING` — real
planned trials. Only one 1900 row and four beyond 2040 are placeholders, nearly all on
`WITHDRAWN` trials that never began. The 184 pre-1990 dates are genuine retrospective
registrations, including NHLBI trials from the 1960s.

**`phaseFromSource` is raw** — 60 variants mixing `PHASE2`, `phase 2`, `APPROVAL` and
`investigative`. Use `trialPhase` (8 values) or `clinicalStage` (13).

**`countries`, `sideEffects` and `year` are effectively empty** — 2,744, 2,744 and 561
rows. `countries` is also dirty, listing `United States` and ` United States` as
separate values among its 128 "distinct" entries.

**`trialLiterature` is PMIDs** — 337,018 values, all digits, 255,869 distinct, with
5,491 nulls inside the lists.
