"""Download pinned Open Targets Platform datasets and record their provenance.

## What pins a release

Open Targets publishes ``release_data_integrity`` at the root of every release:
a SHA-1 for each of ~36,800 files, with its own ``.sha1`` alongside. That is a
better pin than any digest this ingest could mint, because it is the publisher's
own statement about the bytes. So:

1. Fetch ``release_data_integrity.sha1``, then the manifest, and check the
   manifest against it. A corrupted or truncated manifest cannot then silently
   "verify" a corrupted download.
2. Download each dataset's Parquet parts and check every one against the
   manifest's SHA-1.
3. Commit ``manifests/<release>-sources.tsv``, recording for each file the
   upstream SHA-1, our own SHA-256, the byte count and the row count, plus the
   digest of the integrity manifest as the release anchor.

Per the repository convention, only the manifest is committed -- never the data.

## Why the file list is discovered, not hardcoded

Datasets are directories. Some hold Spark output whose part names embed a
per-release UUID (``part-00000-5581d2c3-...-c000.snappy.parquet``), others a
single ``<name>.parquet``. Hardcoding either would break on the next release for
no good reason, so the parts are read from the FTP listing and then pinned by
digest, which is what actually identifies the bytes.

## A trap worth knowing

The release's own ``manifest.json`` reports the overall build ``result``, and for
26.06 that value is ``failure``. The single failed step is ``pos_tarballs`` --
packaging the release into tarballs -- and every step producing the datasets here
succeeded. The overall flag is recorded in ``release.json`` rather than gating
the ingest, but it is not evidence the data is bad, and it should not be read as
such.

Usage:
    python -m opentargets.download_sources --release 26.06
    python -m opentargets.download_sources --release 26.06 --verify   # no download
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .common import (
    DATASETS,
    DEFAULT_RELEASE,
    FTP_BASE,
    INTEGRITY_FILE,
    INTEGRITY_SHA1_FILE,
    OUTPUT_DIR,
    IngestError,
    log,
)

USER_AGENT = "sagebrain-tap-opentargets-ingest"
TIMEOUT = 900

#: Datasets this ingest pins. Everything in DATASETS -- including the ones not
#: yet projected to RDF, because a pin costs nothing and an unpinned input that
#: a later pass starts reading is a silent reproducibility hole.
WANTED = tuple(DATASETS)

HREF = re.compile(r'href="([^"?/][^"]*)"')


def _request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, **kwargs)


def _read(url: str) -> bytes:
    with urllib.request.urlopen(_request(url), timeout=TIMEOUT) as response:
        return response.read()


def _download(url: str, destination: Path) -> None:
    """Download to a ``.part`` file and rename, so an interrupted run leaves no
    truncated file that a later ``--verify`` would have to catch."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    with urllib.request.urlopen(_request(url), timeout=TIMEOUT) as response, \
            open(temporary, "wb") as out:
        shutil.copyfileobj(response, out, length=1 << 20)
    temporary.replace(destination)


def _digest(path: Path, algorithm: str, chunk: int = 1 << 20) -> str:
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def list_parquet_parts(base: str, dataset: str) -> list[str]:
    """Read one dataset's Parquet part names from the FTP directory listing."""
    url = f"{base}/{OUTPUT_DIR}/{dataset}/"
    try:
        listing = _read(url).decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        raise IngestError(f"Could not list {url}: {error}") from error
    parts = sorted({name for name in HREF.findall(listing) if name.endswith(".parquet")})
    if not parts:
        raise IngestError(
            f"{url} lists no .parquet files. The dataset was renamed or removed in "
            f"this release -- Open Targets reorganises datasets between releases."
        )
    return parts


def fetch_integrity_manifest(base: str, destination: Path) -> dict[tuple[str, str], str]:
    """Download upstream's integrity manifest, verify it, and index it.

    Returns ``{(dataset, filename): sha1}`` for files under ``output/``.
    """
    expected = _read(f"{base}/{INTEGRITY_SHA1_FILE}").decode("utf-8").split()[0]
    log(f"Upstream integrity manifest should be sha1 {expected}")
    _download(f"{base}/{INTEGRITY_FILE}", destination)
    actual = _digest(destination, "sha1")
    if actual != expected:
        raise IngestError(
            f"{INTEGRITY_FILE} is sha1 {actual}, but {INTEGRITY_SHA1_FILE} says "
            f"{expected}. Re-download: an unverified manifest cannot be used to "
            f"verify anything else."
        )
    log(f"  manifest verified ({sum(1 for _ in open(destination)):,} entries)")

    index: dict[tuple[str, str], str] = {}
    prefix = f"./{OUTPUT_DIR}/"
    with open(destination, encoding="utf-8") as handle:
        for line in handle:
            sha1, _, path = line.strip().partition("  ")
            if not path.startswith(prefix):
                continue
            dataset, _, filename = path[len(prefix):].partition("/")
            if filename:
                index[(dataset, filename)] = sha1
    return index


def count_rows(directory: Path) -> int:
    import pyarrow.dataset as pads

    return pads.dataset(directory).count_rows()


def write_manifest(path: Path, release: str, anchor: str, published: str,
                   rows: dict[str, int], files: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as out:
        out.write(f"# Open Targets Platform {release} source manifest\n")
        out.write(f"# release: {FTP_BASE.format(release=release)}/{OUTPUT_DIR}/\n")
        out.write(f"# provenance anchor: sha1({INTEGRITY_FILE}) = {anchor}\n")
        out.write(f"# published: {published}  license: CC0-1.0\n")
        out.write("# sha1 is upstream's own digest; sha256 is ours over the same bytes.\n")
        out.write("# rows is the dataset total, repeated on each of its parts.\n")
        out.write("dataset\tfilename\tbytes\trows\tsha1_upstream\tsha256\n")
        for entry in files:
            out.write(
                f"{entry['dataset']}\t{entry['filename']}\t{entry['bytes']}\t"
                f"{rows[entry['dataset']]}\t{entry['sha1']}\t{entry['sha256']}\n"
            )
    log(f"Wrote source manifest -> {path}")


def read_manifest(path: Path) -> list[dict]:
    if not path.exists():
        raise IngestError(
            f"No committed manifest at {path}. Run without --verify first to "
            f"download the release and write it."
        )
    entries = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#") or line.startswith("dataset\t"):
                continue
            dataset, filename, size, rows, sha1, sha256 = line.rstrip("\n").split("\t")
            entries.append({"dataset": dataset, "filename": filename,
                            "bytes": int(size), "rows": int(rows),
                            "sha1": sha1, "sha256": sha256})
    if not entries:
        raise IngestError(f"{path} has a header but no entries.")
    return entries


def verify_local(input_dir: Path, manifest_path: Path) -> int:
    """Re-hash the local copies against the committed manifest.

    Never edits the pin. A release changing under a stable path means the
    contents changed, which is a thing to review, not to accept automatically.
    """
    entries = read_manifest(manifest_path)
    expected_rows = {e["dataset"]: e["rows"] for e in entries}
    intact: dict[str, int] = {dataset: 0 for dataset in expected_rows}
    problems = 0

    for entry in entries:
        path = input_dir / entry["dataset"] / entry["filename"]
        if not path.exists():
            log(f"  MISSING  {entry['dataset']}/{entry['filename']}")
            problems += 1
            continue
        size = path.stat().st_size
        sha256 = _digest(path, "sha256")
        if size != entry["bytes"] or sha256 != entry["sha256"]:
            log(f"  DRIFTED  {entry['dataset']}/{entry['filename']}: "
                f"{size} bytes / {sha256} vs manifest {entry['bytes']} / {entry['sha256']}")
            problems += 1
        else:
            intact[entry["dataset"]] += 1

    # Row counts are a second, independent check: bytes matching proves the parts
    # are the pinned ones, and the row total proves no part of the dataset is
    # simply absent from disk -- which byte-level checks of present files cannot see.
    for dataset, count in sorted(intact.items()):
        if count == 0:
            continue
        actual = count_rows(input_dir / dataset)
        if actual != expected_rows[dataset]:
            log(f"  DRIFTED  {dataset}: {actual:,} rows vs manifest {expected_rows[dataset]:,}")
            problems += 1
        else:
            log(f"  OK       {dataset} ({count} file(s), {actual:,} rows)")
    return problems


def write_release_json(path: Path, release: str, anchor: str, published: str,
                       build_result: str, rows: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "Open Targets Platform",
        "release": release,
        "ftp_base": FTP_BASE.format(release=release),
        "published": published,
        "license": "CC0-1.0",
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "citation_pmid": "39657122",
        "provenance_anchor": {"file": INTEGRITY_FILE, "sha1": anchor},
        # Recorded, deliberately not a gate: at 26.06 this is "failure" because
        # the `pos_tarballs` packaging step failed. Every dataset step succeeded.
        "upstream_build_result": build_result,
        "datasets": {name: {"rows": rows[name], "projected": DATASETS[name].projected,
                            "note": DATASETS[name].note}
                     for name in sorted(rows)},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log(f"Wrote release metadata -> {path}")


def upstream_metadata(base: str) -> tuple[str, str]:
    """``(published_date, build_result)`` from croissant.json and manifest.json.

    croissant.json is the release's machine-readable dataset description and is
    small; manifest.json is 21 MB of build logs, so only its top-level
    ``result`` is read, by streaming the first chunk rather than parsing it.
    """
    published, build_result = "", "unknown"
    try:
        croissant = json.loads(_read(f"{base}/croissant.json"))
        published = str(croissant.get("datePublished", ""))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as error:
        log(f"  could not read croissant.json ({error}); publication date unrecorded")
    try:
        with urllib.request.urlopen(_request(f"{base}/manifest.json"), timeout=TIMEOUT) as r:
            head = r.read(4096).decode("utf-8", "replace")
        match = re.search(r'"result"\s*:\s*"([^"]+)"', head)
        if match:
            build_result = match.group(1)
    except (urllib.error.URLError, OSError) as error:
        log(f"  could not read manifest.json ({error}); build result unrecorded")
    return published, build_result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", default=DEFAULT_RELEASE,
                        help=f"Open Targets release (default: {DEFAULT_RELEASE})")
    parser.add_argument("--outdir", type=Path, default=None,
                        help="Where the datasets go (default: opentargets/input/<release>)")
    parser.add_argument("--manifest-dir", type=Path, default=Path("opentargets/manifests"),
                        help="Where the committed manifests go")
    parser.add_argument("--datasets", nargs="+", default=list(WANTED),
                        choices=list(DATASETS), help="Subset to fetch (default: all)")
    parser.add_argument("--verify", action="store_true",
                        help="Re-hash existing local copies against the committed "
                             "manifest and exit non-zero on drift. No download, no "
                             "network, never edits the pin.")
    args = parser.parse_args()

    base = FTP_BASE.format(release=args.release)
    outdir = args.outdir or Path("opentargets/input") / args.release
    manifest_path = args.manifest_dir / f"{args.release}-sources.tsv"

    if args.verify:
        log(f"Verifying {outdir} against {manifest_path}")
        problems = verify_local(outdir, manifest_path)
        if problems:
            log(f"\n{problems} problem(s). Review the upstream release before "
                f"updating the manifest.")
            return 1
        log("\nAll files match the committed manifest.")
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    log(f"Open Targets {args.release}  <{base}/>")
    integrity_path = outdir / INTEGRITY_FILE
    index = fetch_integrity_manifest(base, integrity_path)
    anchor = _digest(integrity_path, "sha1")
    published, build_result = upstream_metadata(base)
    log(f"  published {published or 'unknown'}, upstream build result: {build_result}")

    files: list[dict] = []
    rows: dict[str, int] = {}
    for dataset in args.datasets:
        parts = list_parquet_parts(base, dataset)
        log(f"{dataset}: {len(parts)} part(s)")
        for filename in parts:
            destination = outdir / dataset / filename
            expected = index.get((dataset, filename))
            if expected is None:
                raise IngestError(
                    f"{dataset}/{filename} is listed in the FTP directory but absent "
                    f"from {INTEGRITY_FILE}. Do not ingest an unpinnable file."
                )
            if destination.exists() and _digest(destination, "sha1") == expected:
                log(f"  have {filename}")
            else:
                log(f"  get  {filename}")
                _download(f"{base}/{OUTPUT_DIR}/{dataset}/{filename}", destination)
                actual = _digest(destination, "sha1")
                if actual != expected:
                    raise IngestError(
                        f"{dataset}/{filename}: sha1 {actual} but the release manifest "
                        f"says {expected}. The release may have rolled over mid-pull; "
                        f"restart the acquisition."
                    )
            files.append({"dataset": dataset, "filename": filename,
                          "bytes": destination.stat().st_size,
                          "sha1": expected,
                          "sha256": _digest(destination, "sha256")})
        rows[dataset] = count_rows(outdir / dataset)
        log(f"  {rows[dataset]:,} rows")

    write_manifest(manifest_path, args.release, anchor, published, rows, files)
    write_release_json(outdir / "release.json", args.release, anchor, published,
                       build_result, rows)
    log(f"\nSources ready in {outdir}")
    log(f"Next: python -m opentargets.verify_schemas --release {args.release}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
