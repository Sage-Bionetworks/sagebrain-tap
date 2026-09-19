"""Gate every Open Targets dataset's Parquet layout before anything is ingested.

Reactome's `verify_columns` equivalent, and it exists for a sharper reason here:
Open Targets reorganises datasets between releases. `clinical_indication` and
`clinical_report` are a recent split out of a single combined dataset, and the
26.06 listing still carries a `clinical_target` alongside them. A pipeline that
discovers such a change by producing an empty graph has wasted the run and, worse,
can look like a source with no data rather than a layout that moved.

So: every column this ingest reads is asserted present, with its Arrow type, and
the controlled vocabularies in `common.py` are checked against what the release
actually contains. A value the ingest has never seen fails here rather than at
transform time, and the report names the constant to extend.

Writes `manifests/<release>-column-verification.md`, which is committed.

Usage:
    python -m opentargets.verify_schemas --release 26.06
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .common import (
    ACTION_TYPES,
    CLINICAL_STAGES,
    DATASETS,
    DEFAULT_RELEASE,
    DISEASE_IRI_BASES,
    DRUG_TYPES,
    TARGET_TYPES,
    IngestError,
    log,
    read_dataset,
)

#: Vocabulary columns to audit, as (dataset, column, allowed, constant name).
#: `None` for `allowed` means "report the values, enforce nothing" -- used for
#: the disease-id prefix audit, which is checked against DISEASE_IRI_BASES keys.
VOCABULARY_CHECKS = [
    ("drug_molecule", "drugType", DRUG_TYPES, "DRUG_TYPES"),
    ("drug_molecule", "maximumClinicalStage", CLINICAL_STAGES, "CLINICAL_STAGES"),
    ("drug_mechanism_of_action", "actionType", ACTION_TYPES, "ACTION_TYPES"),
    ("drug_mechanism_of_action", "targetType", TARGET_TYPES, "TARGET_TYPES"),
    ("clinical_indication", "maxClinicalStage", CLINICAL_STAGES, "CLINICAL_STAGES"),
    # Audited even though clinical_report is not projected: it is the only dataset
    # using PHASE_4 and WITHDRAWAL, and leaving it out of the gate meant the
    # vocabulary looked complete while being short two values.
    ("clinical_report", "clinicalStage", CLINICAL_STAGES, "CLINICAL_STAGES"),
]


def verify_columns(input_dir: Path) -> list[dict]:
    """Assert every required column is present, and record its Arrow type."""
    report = []
    for name, spec in DATASETS.items():
        # read_dataset applies the required-column gate and raises with the
        # missing names; this call is where a moved dataset stops the run.
        data = read_dataset(input_dir, name)
        types = {field.name: str(field.type) for field in data.schema}
        report.append({
            "dataset": name,
            "rows": data.count_rows(),
            "columns_present": len(types),
            "required": sorted(spec.required_columns),
            "types": {column: types[column] for column in sorted(spec.required_columns)},
            "projected": spec.projected,
        })
        log(f"  {name}: {len(spec.required_columns)} required column(s) present "
            f"of {len(types)}, {data.count_rows():,} rows")
    return report


def verify_vocabularies(input_dir: Path) -> tuple[list[dict], list[str]]:
    """Check each controlled vocabulary against the release's actual values."""
    import pyarrow.compute as pc

    findings, failures = [], []
    for dataset, column, allowed, constant in VOCABULARY_CHECKS:
        data = read_dataset(input_dir, dataset, [column])
        table = data.to_table(columns=[column])
        counts = pc.value_counts(table.column(column).combine_chunks())
        observed = {}
        for item in counts:
            value = item["values"].as_py()
            if value is not None:
                observed[value] = item["counts"].as_py()
        unknown = sorted(set(observed) - allowed)
        unused = sorted(allowed - set(observed))
        findings.append({"dataset": dataset, "column": column, "constant": constant,
                         "observed": observed, "unknown": unknown, "unused": unused})
        if unknown:
            failures.append(
                f"{dataset}.{column}: {len(unknown)} value(s) not in {constant}: "
                f"{', '.join(repr(v) for v in unknown[:10])}"
            )
            log(f"  FAIL {dataset}.{column}: unknown {unknown[:5]}")
        else:
            note = f", {len(unused)} known-but-unused" if unused else ""
            log(f"  OK   {dataset}.{column}: {len(observed)} value(s) all known{note}")
    return findings, failures


def verify_disease_prefixes(input_dir: Path) -> tuple[dict, list[str]]:
    """Every disease-id prefix used by indications must have an IRI base.

    Checked against the ids `clinical_indication` actually uses, not against all
    47,080 terms in `disease`: an unmapped prefix only matters if something
    points at it, and gating on the wider set would fail on terms this ingest
    never emits.
    """
    import pyarrow.compute as pc

    data = read_dataset(input_dir, "clinical_indication", ["diseaseId"])
    ids = data.to_table(columns=["diseaseId"]).column("diseaseId").to_pylist()
    counts: dict[str, int] = {}
    for value in ids:
        if not value:
            continue
        prefix = value.partition("_")[0]
        counts[prefix] = counts.get(prefix, 0) + 1
    unknown = sorted(set(counts) - set(DISEASE_IRI_BASES))
    failures = []
    if unknown:
        failures.append(
            f"clinical_indication.diseaseId: no IRI base for prefix(es) "
            f"{', '.join(unknown)}. Add them to DISEASE_IRI_BASES."
        )
        log(f"  FAIL disease id prefixes: unmapped {unknown}")
    else:
        log(f"  OK   disease id prefixes: {len(counts)} prefix(es), all mapped")
    return counts, failures


def render_report(release: str, columns: list[dict], vocabularies: list[dict],
                  disease_prefixes: dict, failures: list[str]) -> str:
    lines = [
        f"# Open Targets {release} column verification",
        "",
        "Generated by `python -m opentargets.verify_schemas`. Every column listed is one",
        "this ingest reads; a column upstream adds is not a problem, a column it removes is.",
        "",
        "## Datasets and required columns",
        "",
    ]
    for entry in columns:
        flag = "" if entry["projected"] else "  *(pinned and gated, not projected)*"
        lines += [f"### `{entry['dataset']}` — {entry['rows']:,} rows"
                  f", {entry['columns_present']} columns{flag}", "",
                  "| Required column | Arrow type |", "| --- | --- |"]
        lines += [f"| `{c}` | `{t}` |" for c, t in entry["types"].items()]
        lines.append("")

    lines += ["## Controlled vocabularies", "",
              "Enforced by `check_vocabulary`; an unlisted value fails the ingest.", ""]
    for entry in vocabularies:
        top = sorted(entry["observed"].items(), key=lambda kv: -kv[1])
        shown = ", ".join(f"`{v}` ({c:,})" for v, c in top[:6])
        more = f", +{len(top) - 6} more" if len(top) > 6 else ""
        lines += [f"- **`{entry['dataset']}.{entry['column']}`** "
                  f"({len(entry['observed'])} values, `{entry['constant']}`): {shown}{more}"]
        if entry["unknown"]:
            lines.append(f"  - **UNKNOWN:** {', '.join(entry['unknown'])}")
        if entry["unused"]:
            lines.append(f"  - defined but unused in this release: "
                         f"{', '.join(f'`{v}`' for v in entry['unused'])}")
    lines.append("")

    lines += ["## Disease id prefixes used by `clinical_indication`", "",
              "| Prefix | Rows | IRI base |", "| --- | --- | --- |"]
    for prefix, count in sorted(disease_prefixes.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{prefix}` | {count:,} | "
                     f"`{DISEASE_IRI_BASES.get(prefix, '— UNMAPPED —')}` |")
    lines += ["", "## Result", "",
              "**FAILED**" if failures else "**PASSED** — every gate above is satisfied.", ""]
    lines += [f"- {failure}" for failure in failures]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", default=DEFAULT_RELEASE)
    parser.add_argument("--indir", type=Path, default=None,
                        help="Dataset directory (default: opentargets/input/<release>)")
    parser.add_argument("--manifest-dir", type=Path, default=Path("opentargets/manifests"))
    parser.add_argument("--report", type=Path, default=None,
                        help="Where the report goes (default: "
                             "<manifest-dir>/<release>-column-verification.md)")
    args = parser.parse_args()

    indir = args.indir or Path("opentargets/input") / args.release
    report_path = args.report or (
        args.manifest_dir / f"{args.release}-column-verification.md")

    log(f"Verifying Open Targets {args.release} layout in {indir}")
    columns = verify_columns(indir)
    vocabularies, vocabulary_failures = verify_vocabularies(indir)
    disease_prefixes, prefix_failures = verify_disease_prefixes(indir)
    failures = vocabulary_failures + prefix_failures

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(args.release, columns, vocabularies, disease_prefixes, failures),
        encoding="utf-8")
    log(f"Wrote {report_path}")

    if failures:
        log("\nLayout verification FAILED:")
        for failure in failures:
            log(f"  {failure}")
        return 1
    log("\nLayout verification passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
