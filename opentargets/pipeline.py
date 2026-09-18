"""Run the Open Targets drug-layer ingest end to end.

Order: download -> verify layout -> molecules -> mechanisms -> indications ->
label index -> load -> acceptance checks. The transforms are independent of each
other (unlike Reactome's, where pathways writes the ID set the next two consume),
so the order among them is only for readable logs.

Acceptance checks run last and gate nothing after themselves, which is the point:
a release that fails them has still written its Turtle, so the failure can be
diagnosed from the output rather than from a rerun.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .common import DEFAULT_RELEASE, log

REPO_ROOT = Path(__file__).resolve().parents[1]


def run(module: str, arguments: list[str]) -> None:
    # cwd=REPO_ROOT because every default path in this ingest is repo-relative
    # ("opentargets/input/<release>"), so running the pipeline from elsewhere
    # would otherwise resolve them against the caller's directory.
    command = [sys.executable, "-m", f"opentargets.{module}", *arguments]
    log(f"\n$ {' '.join(command[2:])}")
    result = subprocess.run(command, cwd=REPO_ROOT)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", default=DEFAULT_RELEASE,
                        help=f"Open Targets release (default: {DEFAULT_RELEASE})")
    parser.add_argument("--input-dir", type=Path,
                        help="Default: opentargets/input/<release>")
    parser.add_argument("--workdir", type=Path,
                        help="Default: opentargets/<release>/data")
    parser.add_argument("--manifest-dir", type=Path, default=Path("opentargets/manifests"))
    parser.add_argument("--hgnc", type=Path, default=None,
                        help="HGNC complete set for the Ensembl->HGNC hop")
    parser.add_argument("--skip-download", action="store_true",
                        help="Reuse downloaded datasets; still verifies them against "
                             "the committed manifest")
    parser.add_argument("--endpoint", default=None,
                        help="Oxigraph server base URL, e.g. http://localhost:7011. "
                             "Loads to <base>/store and checks against <base>/query")
    parser.add_argument("--skip-acceptance", action="store_true")
    args = parser.parse_args()

    release = ["--release", args.release]
    indir = ["--indir", str(args.input_dir)] if args.input_dir else []
    workdir = ["--workdir", str(args.workdir)] if args.workdir else []
    manifests = ["--manifest-dir", str(args.manifest_dir)]

    if args.skip_download:
        # Verify rather than trust: the point of the committed manifest is that
        # reusing a local copy is still a checked operation.
        run("download_sources", release + manifests + ["--verify"] +
            (["--outdir", str(args.input_dir)] if args.input_dir else []))
    else:
        run("download_sources", release + manifests +
            (["--outdir", str(args.input_dir)] if args.input_dir else []))

    run("verify_schemas", release + indir + manifests)
    run("transform_molecules", release + indir + workdir)
    run("transform_mechanisms", release + indir + workdir +
        (["--hgnc", str(args.hgnc)] if args.hgnc else []))
    run("transform_indications", release + indir + workdir)
    run("export_label_index", release + indir + workdir)

    ttl_dir = ["--ttl-dir", str(Path(args.workdir) / "rdf")] if args.workdir else []
    load = release + ttl_dir
    if args.endpoint:
        load += ["--endpoint", args.endpoint.rstrip("/") + "/store"]
    run("load_graph", load)

    if not args.skip_acceptance:
        check = release + manifests
        if args.endpoint:
            check += ["--endpoint", args.endpoint.rstrip("/") + "/query"]
        elif args.workdir:
            check += ["--store", str(Path(args.workdir) / "store")]
        run("acceptance_checks", check)

    log(f"\nOpen Targets {args.release} ingest complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
