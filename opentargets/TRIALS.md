# Open Targets clinical trial layer

This document records the scope of a trial layer over `clinical_report`. The
decisions below are settled; **the transform is not written**. `clinical_report` is
downloaded, pinned and column-gated today, and projects nothing into RDF.

See [DESIGN.md](DESIGN.md) for the drug-layer model that this would extend,
[README.md](README.md) for operations, and [manifests/](manifests/) for the pinned
release layout.

## Scope

**Only `type = CLINICAL_TRIAL`.** That is 230,990 of the 289,955 records. The other
three types — curated resources, drug labels and regulatory records — are provenance
about a drug rather than trial evidence, and they have no usable identity: trial IDs
are uniformly `nct<digits>`, with no exceptions, while the rest are a mix of DailyMed
UUIDs, sha256 hashes and raw strings such as `019909s020lbl.pdf` or a bare URL. Only
trials get a CURIE that survives a release.

**Only trials that join an emitted molecule.** Keep the 193,469 trials (83.8%) with at
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

Range is a separate check from precision, and future dates are mostly real: 150 trials
start in 2027–2030, 121 of them `NOT_YET_RECRUITING`. Only a handful are placeholders —
one 1900-01-31 row and four beyond 2040, up to 2099-01-01, nearly all on `WITHDRAWN`
trials that never began. Gate outside 1950 through ten years past the release date,
which isolates exactly those without discarding planned starts. The 184 pre-1990 dates
are genuine retrospective registrations, including NHLBI trials from the 1960s.

**Diseases.** Trials reference 3,841 terms, 310 of which no indication references
(184 MONDO, 59 HP, 56 EFO, 4 OBA, 4 GO, 3 Orphanet). Emit those nodes too, which widens
the disease pass from "terms referenced by indications" to terms referenced by either.
A further 50,971 in-scope trials map no disease at all and become drug-only trial nodes.

**What this adds.** 80,145 trials are new to the graph; the other 113,324 are already
referenced by indication associations, which can then carry evidence edges instead of
only `sagebrain:clinical_report_count`. 21,876 carry a stop reason, and every record
with `trialWhyStopped` free text also carries a category from a 13-value vocabulary
(Insufficient_Enrollment, Business_Administrative, Negative, Safety_Sideeffects and
others) — the trial history that [DESIGN.md](DESIGN.md) §4 notes an indication's maximum stage cannot preserve.
`qualityControls` has four values, of which only `PHASE_IV_NOT_APPROVED` and
`INDIRECT_PRIMARY_PURPOSE` gate `clinical_indication`; emitting all four lets a consumer
reproduce or relax that filter. Both new vocabularies need constants and a gate in
[verify_schemas.py](verify_schemas.py) like the existing ones.

**Fields left out.** `countries` and `sideEffects` are filled on 2,744 rows each and
`year` on 561; `countries` is also dirty, listing `United States` and ` United States`
as separate values. `phaseFromSource` holds 60 unnormalised variants mixing `PHASE2`,
`phase 2` and `investigative` — read `trialPhase` or `clinicalStage` instead.
