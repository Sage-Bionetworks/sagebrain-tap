"""Download versioned Reactome flat files and HGNC mappings; record checksums.

The Zenodo version DOI anchors provenance. By default, fetch individual files
from download.reactome.org/<version>; --from-zenodo extracts the full archive."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .common import IngestError, log


ZENODO_CONCEPT_RECID = "12763591"  # "Reactome Download Directory", concept record
ZENODO_API = "https://zenodo.org/api"

WORKING_BASES = (
    "https://download.reactome.org/{version}",
    "https://reactome.org/download/{version}",
)

# The four files of plan section 3, plus the optional cross-reference files.
CORE_FILES = (
    "ReactomePathways.txt",
    "ReactomePathwaysRelation.txt",
    "UniProt2Reactome_All_Levels.txt",
    "Pathways2GoTerms_human.txt",
)
OPTIONAL_FILES = (
    "Ensembl2Reactome_All_Levels.txt",
    "NCBI2Reactome_All_Levels.txt",
)

# UniProt -> HGNC resolution (open decision 2: HGNC subject, accession retained).
HGNC_URL = "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt"
HGNC_FILENAME = "hgnc_complete_set.txt"


@dataclass
class ZenodoRelease:
    version: str
    doi: str
    publication_date: str
    license_id: str
    zip_url: str
    zip_size: int
    zip_md5: str

    def as_dict(self) -> dict:
        return dict(
            version=self.version,
            doi=self.doi,
            doi_url=f"https://doi.org/{self.doi}",
            publication_date=self.publication_date,
            license_id=self.license_id,
            zip_url=self.zip_url,
            zip_size=self.zip_size,
            zip_md5=self.zip_md5,
            concept_doi=f"10.5281/zenodo.{ZENODO_CONCEPT_RECID}",
        )


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


ZENODO_PAGE_SIZE = 25  # unauthenticated maximum; larger values are a 400


def _zenodo_versions() -> list[dict]:
    """Page through every version of the Reactome Download Directory concept.

    Paged rather than fetched in one shot: Zenodo caps unauthenticated page size
    at 25 and Reactome is already past that, so a single request would silently
    stop returning older releases.
    """
    records: list[dict] = []
    page = 1
    while True:
        url = (
            f"{ZENODO_API}/records?q=conceptrecid:{ZENODO_CONCEPT_RECID}"
            f"&all_versions=true&size={ZENODO_PAGE_SIZE}&page={page}&sort=-mostrecent"
        )
        hits = _get_json(url).get("hits", {}).get("hits", [])
        records.extend(hits)
        if len(hits) < ZENODO_PAGE_SIZE:
            return records
        page += 1
        if page > 40:  # backstop; Reactome publishes quarterly
            return records


def resolve_zenodo_release(version: str) -> ZenodoRelease:
    """Find the Zenodo record for a Reactome release.

    Resolved through the concept record rather than hardcoded per release, so
    each quarterly refresh picks up its own DOI without a code change.
    """
    wanted = version if version.startswith("v") else f"v{version}"
    hits = _zenodo_versions()
    if not hits:
        raise IngestError("Zenodo returned no versions for the Reactome Download Directory.")

    matches = [h for h in hits if str(h.get("metadata", {}).get("version")) == wanted]
    if not matches:
        available = sorted(str(h.get("metadata", {}).get("version")) for h in hits)
        raise IngestError(
            f"No Zenodo record for Reactome {wanted}. Available: {', '.join(available)}"
        )
    record = matches[0]
    metadata = record.get("metadata", {})

    zips = [f for f in record.get("files", []) if str(f.get("key", "")).endswith(".zip")]
    if not zips:
        raise IngestError(f"Zenodo record for {wanted} has no .zip payload.")
    payload = zips[0]
    checksum = str(payload.get("checksum", ""))

    license_field = metadata.get("license")
    license_id = (
        license_field.get("id") if isinstance(license_field, dict) else str(license_field or "")
    )

    return ZenodoRelease(
        version=wanted,
        doi=record.get("doi", ""),
        publication_date=str(metadata.get("publication_date", "")),
        license_id=license_id or "cc-by-4.0",
        zip_url=payload.get("links", {}).get("self", ""),
        zip_size=int(payload.get("size", 0)),
        zip_md5=checksum.split(":", 1)[-1] if checksum else "",
    )


def _md5(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "sagebrain-reactome-ingest"})
    with urllib.request.urlopen(request, timeout=600) as response, open(temporary, "wb") as out:
        shutil.copyfileobj(response, out, length=1 << 20)
    temporary.replace(destination)


def _looks_like_tsv(path: Path) -> bool:
    """Reject HTML error pages served with a 200.

    ``reactome.org/download/<version>/`` answers requests for older releases with
    an HTML error page and HTTP 200, so status codes alone are not enough -- an
    unchecked fetch would write a web page into the pipeline and fail much later,
    somewhere less obvious.
    """
    with open(path, "rb") as handle:
        head = handle.read(4096)
    if head.lstrip()[:1] == b"<":
        return False
    first_line = head.split(b"\n", 1)[0]
    return b"\t" in first_line


def fetch_working_path(filename: str, version: str, destination: Path) -> str:
    """Download one file from the working path, returning the URL that served it."""
    last_error: Exception | None = None
    for template in WORKING_BASES:
        url = f"{template.format(version=version)}/{filename}"
        try:
            _download(url, destination)
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            last_error = error
            continue
        if not _looks_like_tsv(destination):
            destination.unlink(missing_ok=True)
            last_error = IngestError(f"{url} served a non-TSV payload (likely an HTML error page)")
            continue
        return url
    raise IngestError(f"Could not fetch {filename} for release {version}: {last_error}")


def fetch_zenodo_zip(release: ZenodoRelease, destination: Path) -> None:
    log(f"Downloading Zenodo payload ({release.zip_size / 1e9:.1f} GB) -- this is not quick.")
    _download(release.zip_url, destination)
    actual = _md5(destination)
    if release.zip_md5 and actual != release.zip_md5:
        raise IngestError(
            f"Zenodo zip md5 mismatch: expected {release.zip_md5}, got {actual}. "
            "Re-download before proceeding."
        )
    log(f"Zenodo payload verified (md5 {actual}).")


class HttpRangeFile(io.RawIOBase):
    """A seekable, read-only file over HTTP range requests.

    Exists so the archive can be enumerated without downloading it. A zip's
    central directory lives at the *end* of the file, so fetching the tail is
    enough for ``zipfile`` to list every member -- a few MB instead of 4.7 GB.
    The tail is prefetched once; anything outside it falls back to a range
    request, so this stays correct even if the directory is larger than expected.
    """

    def __init__(self, url: str, tail_bytes: int = 16 << 20):
        self.url = url
        self._pos = 0
        self.size = self._remote_size()
        self._tail_start = max(0, self.size - tail_bytes)
        self._tail = self._fetch(self._tail_start, self.size - 1)

    def _remote_size(self) -> int:
        request = urllib.request.Request(
            self.url, method="HEAD", headers={"User-Agent": "sagebrain-reactome-ingest"}
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            length = response.headers.get("Content-Length")
        if not length:
            raise IngestError(f"{self.url} did not report a Content-Length; cannot range-read it.")
        return int(length)

    def _fetch(self, start: int, end: int) -> bytes:
        request = urllib.request.Request(
            self.url,
            headers={"Range": f"bytes={start}-{end}", "User-Agent": "sagebrain-reactome-ingest"},
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            if response.status != 206:
                raise IngestError(
                    f"{self.url} ignored the Range header (status {response.status}); "
                    "remote enumeration needs a server that supports range requests."
                )
            return response.read()

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        else:
            self._pos = self.size + offset
        self._pos = max(0, min(self._pos, self.size))
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = self.size - self._pos
        size = min(size, self.size - self._pos)
        if size <= 0:
            return b""
        start, end = self._pos, self._pos + size - 1
        if start >= self._tail_start:
            offset = start - self._tail_start
            data = self._tail[offset:offset + size]
        else:
            data = self._fetch(start, end)
        self._pos += len(data)
        return data


def enumerate_zip_remote(url: str, manifest_path: Path) -> dict[str, str]:
    """Enumerate the Zenodo archive over HTTP, without downloading it."""
    log("Enumerating the Zenodo archive over HTTP range requests (no full download) ...")
    remote = HttpRangeFile(url)
    log(f"  archive is {remote.size / 1e9:.2f} GB")
    with zipfile.ZipFile(remote) as archive:
        return _write_archive_manifest(archive, manifest_path)


def _write_archive_manifest(archive: zipfile.ZipFile, manifest_path: Path) -> dict[str, str]:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    located: dict[str, str] = {}
    entries = 0
    with open(manifest_path, "w", encoding="utf-8") as out:
        out.write("path_in_archive\tsize_bytes\tcompressed_bytes\tcrc32\n")
        for info in sorted(archive.infolist(), key=lambda i: i.filename):
            if info.is_dir():
                continue
            entries += 1
            out.write(
                f"{info.filename}\t{info.file_size}\t{info.compress_size}\t{info.CRC:08x}\n"
            )
            name = Path(info.filename).name
            if name in CORE_FILES or name in OPTIONAL_FILES:
                located[name] = info.filename
    log(f"  {entries:,} files in the archive; wrote manifest -> {manifest_path}")
    return located


def enumerate_zip(zip_path: Path, manifest_path: Path) -> dict[str, str]:
    """First-run task (plan section 2): commit the zip's contents to the repo.

    Nobody should have to download 4.7 GB again to find out what is in it.
    Reads the central directory only -- no extraction.
    """
    with zipfile.ZipFile(zip_path) as archive:
        return _write_archive_manifest(archive, manifest_path)


def extract_from_zip(zip_path: Path, members: dict[str, str], outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for name, member in members.items():
            log(f"  extracting {member}")
            with archive.open(member) as source, open(outdir / name, "wb") as out:
                shutil.copyfileobj(source, out, length=1 << 20)


def write_manifest(
    manifest_path: Path,
    release: ZenodoRelease,
    files: list[tuple[str, Path, str]],
) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as out:
        out.write(f"# Reactome {release.version} source manifest\n")
        out.write(f"# provenance (authoritative): https://doi.org/{release.doi}\n")
        out.write(f"# concept DOI: https://doi.org/10.5281/zenodo.{ZENODO_CONCEPT_RECID}\n")
        out.write(f"# published: {release.publication_date}  license: {release.license_id}\n")
        out.write("filename\tbytes\tmd5\tretrieved_from\n")
        for name, path, url in files:
            out.write(f"{name}\t{path.stat().st_size}\t{_md5(path)}\t{url}\n")
    log(f"Wrote source manifest -> {manifest_path}")


def write_release_json(path: Path, release: ZenodoRelease) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(release.as_dict(), indent=2) + "\n", encoding="utf-8")
    log(f"Wrote release metadata -> {path}")


def verify_against_zenodo(outdir: Path, zip_path: Path, members: dict[str, str]) -> None:
    """Plan section 2 step 3: checksum the working-path copies against Zenodo.

    A mismatch means the release rolled over mid-pull -- restart.
    """
    mismatches = []
    with zipfile.ZipFile(zip_path) as archive:
        for name, member in members.items():
            local = outdir / name
            if not local.exists():
                continue
            digest = hashlib.md5()
            with archive.open(member) as source:
                while block := source.read(1 << 20):
                    digest.update(block)
            if digest.hexdigest() != _md5(local):
                mismatches.append(name)
    if mismatches:
        raise IngestError(
            "Working-path copies differ from the Zenodo release for: "
            + ", ".join(mismatches)
            + ". The release rolled over mid-pull -- restart the acquisition."
        )
    log("All working-path copies match the Zenodo release.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True, help="Reactome release, e.g. 97")
    parser.add_argument("--outdir", type=Path, default=None,
                        help="Working directory for extracted sources (default: reactome/input/v<version>)")
    parser.add_argument("--manifest-dir", type=Path, default=Path("reactome/manifests"),
                        help="Where the committed manifests go (default: reactome/manifests)")
    parser.add_argument("--from-zenodo", action="store_true",
                        help="Pull the 4.7 GB Zenodo zip, enumerate it, and extract the source files")
    parser.add_argument("--enumerate-remote", action="store_true",
                        help="Write the archive-contents manifest by reading the zip's central "
                             "directory over HTTP range requests, without downloading it")
    parser.add_argument("--verify-against-zenodo", action="store_true",
                        help="Checksum existing working-path copies against the Zenodo release")
    parser.add_argument("--keep-zip", action="store_true", help="Do not delete the Zenodo zip afterwards")
    parser.add_argument("--with-optional", action="store_true",
                        help="Also fetch Ensembl2Reactome / NCBI2Reactome cross-references")
    parser.add_argument("--skip-hgnc", action="store_true",
                        help="Skip the HGNC complete set (needed for the UniProt->HGNC hop)")
    args = parser.parse_args()

    version = args.version.lstrip("v")
    outdir = args.outdir or Path("reactome/input") / f"v{version}"
    outdir.mkdir(parents=True, exist_ok=True)

    log(f"Resolving Zenodo record for Reactome v{version} ...")
    release = resolve_zenodo_release(version)
    log(f"  {release.version}: {release.doi} ({release.publication_date}, {release.license_id})")

    wanted = list(CORE_FILES) + (list(OPTIONAL_FILES) if args.with_optional else [])
    fetched: list[tuple[str, Path, str]] = []
    zip_path = outdir / "reactome-download-directory.zip"
    archive_manifest = args.manifest_dir / f"v{version}-archive-contents.tsv"

    if args.enumerate_remote:
        members = enumerate_zip_remote(release.zip_url, archive_manifest)
        missing = [name for name in CORE_FILES if name not in members]
        if missing:
            raise IngestError("The Zenodo archive is missing required files: " + ", ".join(missing))
        log("All four required files located in the archive:")
        for name in CORE_FILES:
            log(f"  {name}  ->  {members[name]}")
        write_release_json(outdir / "release.json", release)
        return 0

    if args.from_zenodo or args.verify_against_zenodo:
        if not zip_path.exists():
            fetch_zenodo_zip(release, zip_path)
        else:
            log(f"Reusing existing {zip_path}")
        members = enumerate_zip(zip_path, archive_manifest)
        missing = [name for name in CORE_FILES if name not in members]
        if missing:
            raise IngestError(
                "The Zenodo archive is missing required files: " + ", ".join(missing)
            )
        log(f"All {len(CORE_FILES)} required files located in the archive.")

        if args.verify_against_zenodo:
            verify_against_zenodo(outdir, zip_path, {n: members[n] for n in CORE_FILES})
        else:
            extract_from_zip(zip_path, {n: members[n] for n in wanted if n in members}, outdir)
            for name in wanted:
                if (outdir / name).exists():
                    fetched.append((name, outdir / name, f"https://doi.org/{release.doi}"))

        if not args.keep_zip and not args.verify_against_zenodo:
            zip_path.unlink(missing_ok=True)
            log("Removed the zip (pass --keep-zip to retain it).")
    else:
        for name in wanted:
            destination = outdir / name
            log(f"Fetching {name} ...")
            url = fetch_working_path(name, version, destination)
            fetched.append((name, destination, url))

    if not args.skip_hgnc and not args.verify_against_zenodo:
        log("Fetching HGNC complete set (UniProt -> HGNC resolution) ...")
        hgnc_path = outdir / HGNC_FILENAME
        _download(HGNC_URL, hgnc_path)
        fetched.append((HGNC_FILENAME, hgnc_path, HGNC_URL))

    if fetched:
        write_manifest(
            args.manifest_dir / f"v{version}-sources.tsv", release, fetched
        )
    write_release_json(outdir / "release.json", release)

    log(f"\nSources ready in {outdir}")
    log("Next: python -m reactome.verify_columns --indir " + str(outdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
