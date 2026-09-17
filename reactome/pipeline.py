"""Download, validate, transform and load a human Reactome release into Oxigraph."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .common import log


REPO_ROOT = Path(__file__).resolve().parents[1]


def run(module: str, arguments: list[str]) -> None:
    command = [sys.executable, "-m", f"reactome.{module}", *arguments]
    log(f"Running {module}")
    subprocess.run(command, cwd=REPO_ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Reactome release, e.g. 97")
    parser.add_argument("--input-dir", type=Path, help="Default: reactome/input/v<version>")
    parser.add_argument("--workdir", type=Path, help="Default: reactome/v<version>/data")
    parser.add_argument("--manifest-dir", type=Path, default=Path("reactome/manifests"))
    parser.add_argument("--from-zenodo", action="store_true", help="Download the full Zenodo archive")
    parser.add_argument("--skip-download", action="store_true", help="Reuse downloaded sources")
    parser.add_argument("--expected-pathways", type=int, help="Human pathway count for this release")
    parser.add_argument("--round-trip", type=int, default=5, help="Content Service checks (0 for offline)")
    parser.add_argument("--endpoint", help="Oxigraph server base URL, e.g. http://localhost:7010")
    args = parser.parse_args()

    version = args.version.lstrip("vV")
    if not version.isdecimal():
        parser.error("--version must be a release number, e.g. 97")
    # Resolve user paths before subprocesses change to the repository root.
    sources = (args.input_dir or REPO_ROOT / "reactome/input" / f"v{version}").resolve()
    workdir = (args.workdir or REPO_ROOT / "reactome" / f"v{version}/data").resolve()
    manifests = args.manifest_dir.resolve()
    rdf = workdir / "rdf"
    store = workdir / "store"

    if not args.skip_download:
        download_args = ["--version", version, "--outdir", str(sources),
                         "--manifest-dir", str(manifests)]
        if args.from_zenodo:
            download_args.append("--from-zenodo")
        run("download_sources", download_args)

    run("verify_columns", ["--indir", str(sources), "--version", version,
                           "--report", str(manifests / f"v{version}-column-verification.md")])
    for module in ("transform_pathways", "transform_associations", "transform_go"):
        run(module, ["--indir", str(sources), "--outdir", str(rdf)])

    load_target = ["--store", str(store)]
    query_target = load_target
    if args.endpoint:
        base = args.endpoint.rstrip("/")
        load_target = ["--endpoint", f"{base}/store"]
        query_target = ["--endpoint", f"{base}/query"]
    run("load_graph", ["--ttl-dir", str(rdf), "--version", version,
                       "--release-json", str(sources / "release.json"), *load_target])
    checks = ["--version", version, "--round-trip", str(args.round_trip),
              "--report", str(manifests / f"v{version}-acceptance.md"), *query_target]
    if args.expected_pathways is not None:
        checks += ["--expected-pathways", str(args.expected_pathways)]
    run("acceptance_checks", checks)
    # After the checks, so a release that failed them never leaves a
    # characteristics manifest claiming to describe a good ingest.
    run("describe_data", ["--version", version, "--ttl-dir", str(rdf),
                          "--sources", str(manifests / f"v{version}-sources.tsv"),
                          "--out", str(manifests / f"v{version}-characteristics.json")])

    log(f"Complete: <urn:sagebrain:reactome:v{version}>; RDF: {rdf}")
    log(f"Query: python -m reactome.queries --version {version} "
        f"{' '.join(query_target)} --canned gene-pathway-count --gene APP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
