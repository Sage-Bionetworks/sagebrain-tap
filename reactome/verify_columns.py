"""Validate Reactome TSV layouts before transforming a release.

Pathways2GoTerms_human.txt has a header; the other core files do not."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .common import (
    EVIDENCE_CODE_TO_ECO,
    SPECIES_NAME_TO_TAXON_ID,
    IngestError,
    log,
    open_maybe_gzip,
)


STABLE_ID = re.compile(r"^R-[A-Z]{3}-\d+(\.\d+)?$")
HSA_STABLE_ID = re.compile(r"^R-HSA-\d+(\.\d+)?$")
UNIPROT_ACCESSION = re.compile(r"^[A-Z0-9]{6,10}(-\d+)?$")
GO_ID = re.compile(r"^GO:\d{7}$")
PATHWAY_URL = re.compile(r"^https?://")

SAMPLE_ROWS = 5000


@dataclass
class ColumnSpec:
    index: int  # 1-based, to match the plan's tables
    name: str
    validate: Callable[[str], bool]
    description: str = ""


@dataclass
class FileSpec:
    filename: str
    expected_fields: int
    has_header: bool
    expected_header: list[str] | None
    columns: list[ColumnSpec]
    note: str = ""


FILE_SPECS = [
    FileSpec(
        filename="ReactomePathways.txt",
        expected_fields=3,
        has_header=False,
        expected_header=None,
        columns=[
            ColumnSpec(1, "stable_id", lambda v: bool(STABLE_ID.match(v))),
            ColumnSpec(2, "pathway_name", lambda v: bool(v.strip())),
            ColumnSpec(3, "species_name", lambda v: v in SPECIES_NAME_TO_TAXON_ID),
        ],
        note="All species. Filter on column 3, never on the stable-ID infix.",
    ),
    FileSpec(
        filename="ReactomePathwaysRelation.txt",
        expected_fields=2,
        has_header=False,
        expected_header=None,
        columns=[
            ColumnSpec(1, "parent_stable_id", lambda v: bool(STABLE_ID.match(v))),
            ColumnSpec(2, "child_stable_id", lambda v: bool(STABLE_ID.match(v))),
        ],
        note="No species column -- must be filtered by ID membership (plan section 8).",
    ),
    FileSpec(
        filename="UniProt2Reactome_All_Levels.txt",
        expected_fields=6,
        has_header=False,
        expected_header=None,
        columns=[
            ColumnSpec(1, "uniprot_accession", lambda v: bool(UNIPROT_ACCESSION.match(v))),
            ColumnSpec(2, "pathway_stable_id", lambda v: bool(STABLE_ID.match(v))),
            ColumnSpec(3, "pathway_browser_url", lambda v: bool(PATHWAY_URL.match(v)),
                       "discarded"),
            ColumnSpec(4, "event_name", lambda v: bool(v.strip())),
            ColumnSpec(5, "evidence_code", lambda v: v in EVIDENCE_CODE_TO_ECO),
            ColumnSpec(6, "species_name", lambda v: v in SPECIES_NAME_TO_TAXON_ID),
        ],
        note="Pre-propagated (_All_Levels): do not apply transitive closure again.",
    ),
    FileSpec(
        filename="Pathways2GoTerms_human.txt",
        expected_fields=3,
        has_header=True,
        expected_header=["Identifier", "Name", "GO_Term"],
        columns=[
            ColumnSpec(1, "pathway_stable_id", lambda v: bool(HSA_STABLE_ID.match(v))),
            ColumnSpec(2, "pathway_name", lambda v: bool(v.strip())),
            ColumnSpec(3, "go_id", lambda v: bool(GO_ID.match(v))),
        ],
        note="HAS A HEADER ROW -- the one exception to the no-header rule. Human-only.",
    ),
]


@dataclass
class FileReport:
    spec: FileSpec
    rows: int = 0
    header_found: list[str] | None = None
    field_counts: dict[int, int] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    samples: dict[str, str] = field(default_factory=dict)
    species_seen: dict[str, int] = field(default_factory=dict)
    evidence_seen: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.violations


def verify_file(path: Path, spec: FileSpec) -> FileReport:
    report = FileReport(spec=spec)
    species_index = next((c.index for c in spec.columns if c.name == "species_name"), None)
    evidence_index = next((c.index for c in spec.columns if c.name == "evidence_code"), None)

    with open_maybe_gzip(path) as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            row = line.split("\t")

            if lineno == 1:
                first = row[0].strip()
                looks_like_header = not (
                    STABLE_ID.match(first) or UNIPROT_ACCESSION.match(first)
                )
                if spec.has_header != looks_like_header:
                    report.violations.append(
                        f"header expectation mismatch: spec says has_header="
                        f"{spec.has_header} but first row is {row!r}"
                    )
                if spec.has_header:
                    report.header_found = row
                    if spec.expected_header and row != spec.expected_header:
                        report.violations.append(
                            f"header is {row!r}, expected {spec.expected_header!r}"
                        )
                    continue

            report.rows += 1
            report.field_counts[len(row)] = report.field_counts.get(len(row), 0) + 1

            if species_index is not None and len(row) >= species_index:
                name = row[species_index - 1]
                report.species_seen[name] = report.species_seen.get(name, 0) + 1
            if evidence_index is not None and len(row) >= evidence_index:
                code = row[evidence_index - 1]
                report.evidence_seen[code] = report.evidence_seen.get(code, 0) + 1

            # Validate a sample: full-file regex validation on a 942k-row file
            # buys nothing over a large sample, and this is a gate, not a scan.
            if report.rows <= SAMPLE_ROWS:
                if len(row) != spec.expected_fields:
                    report.violations.append(
                        f"line {lineno}: {len(row)} fields, expected {spec.expected_fields}"
                    )
                    continue
                for column in spec.columns:
                    value = row[column.index - 1]
                    report.samples.setdefault(column.name, value)
                    if not column.validate(value):
                        report.violations.append(
                            f"line {lineno}: column {column.index} ({column.name}) "
                            f"failed validation: {value!r}"
                        )

    counts = set(report.field_counts)
    if counts and counts != {spec.expected_fields}:
        report.violations.append(
            f"inconsistent field counts across the file: {sorted(counts)}"
        )
    if report.rows == 0:
        report.violations.append("file contains no data rows")
    return report


def render(report: FileReport) -> str:
    spec = report.spec
    lines = [f"### `{spec.filename}`", ""]
    lines.append(f"- rows (excluding header): **{report.rows:,}**")
    lines.append(f"- header row: **{'yes -- ' + str(report.header_found) if report.header_found else 'no'}**")
    lines.append(f"- field count: {sorted(report.field_counts)} (expected {spec.expected_fields})")
    if spec.note:
        lines.append(f"- note: {spec.note}")
    lines.append("")
    lines.append("| Col | Name | Example |")
    lines.append("|---|---|---|")
    for column in spec.columns:
        example = report.samples.get(column.name, "")
        if len(example) > 60:
            example = example[:57] + "..."
        suffix = f" _({column.description})_" if column.description else ""
        lines.append(f"| {column.index} | `{column.name}`{suffix} | `{example}` |")
    lines.append("")
    if report.species_seen:
        top = sorted(report.species_seen.items(), key=lambda kv: -kv[1])
        lines.append(f"- species present: {len(top)} "
                     f"(top: {', '.join(f'{n} ({c:,})' for n, c in top[:3])})")
    if report.evidence_seen:
        codes = sorted(report.evidence_seen.items(), key=lambda kv: -kv[1])
        lines.append(f"- evidence codes: {', '.join(f'`{c}` ({n:,})' for c, n in codes)}")
    if report.violations:
        lines.append("")
        lines.append("**VIOLATIONS**")
        for violation in report.violations[:20]:
            lines.append(f"- {violation}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--indir", type=Path, required=True, help="Directory holding the source files")
    parser.add_argument("--report", type=Path, default=None, help="Write a markdown report here")
    parser.add_argument("--version", default="", help="Release label for the report header")
    args = parser.parse_args()

    reports = []
    for spec in FILE_SPECS:
        path = args.indir / spec.filename
        if not path.exists():
            raise IngestError(f"Missing source file {path}. Run download_sources.py first.")
        log(f"Verifying {spec.filename} ...")
        report = verify_file(path, spec)
        reports.append(report)
        status = "OK" if report.ok else f"FAILED ({len(report.violations)} violations)"
        log(f"  {status}: {report.rows:,} rows")

    if args.report:
        version = args.version or args.indir.name.lstrip("v")
        body = [
            f"# Reactome v{version} -- column-layout verification",
            "",
            "Generated by `reactome/verify_columns.py`. This is the step 2 gate:",
            "the transforms refuse to run against a layout that has not been verified.",
            "",
        ]
        body.extend(render(r) for r in reports)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text("\n".join(body), encoding="utf-8")
        log(f"Wrote report -> {args.report}")

    failed = [r for r in reports if not r.ok]
    if failed:
        log("\nColumn verification FAILED for: " + ", ".join(r.spec.filename for r in failed))
        for report in failed:
            for violation in report.violations[:10]:
                log(f"  {report.spec.filename}: {violation}")
        return 1

    log("\nAll four source files match their verified layouts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
