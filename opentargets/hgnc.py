"""Acquire and verify the HGNC snapshot pinned for an Open Targets release."""

from __future__ import annotations

import hashlib
import json
import shutil
import urllib.request
from pathlib import Path

from .common import HgncResolver, IngestError, log

HGNC_FILENAME = "hgnc_complete_set.txt"
MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"


def read_pin(release: str, manifest_dir: Path = MANIFEST_DIR) -> dict:
    path = manifest_dir / f"{release}-hgnc.json"
    if not path.exists():
        raise IngestError(
            f"No HGNC pin at {path}. Add a reviewed archive URL, snapshot date, "
            "byte count and SHA-256 for this release before ingesting."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def verify(path: Path, pin: dict) -> None:
    if not path.is_file():
        raise IngestError(
            f"HGNC complete set not found at {path}. Run "
            "opentargets.download_sources first, or pass --hgnc with a copy "
            "of the pinned snapshot."
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    if path.stat().st_size != pin["bytes"] or digest.hexdigest() != pin["sha256"]:
        raise IngestError(
            f"HGNC checksum mismatch at {path}: expected {pin['sha256']} "
            f"({pin['bytes']} bytes), got {digest.hexdigest()} "
            f"({path.stat().st_size} bytes). Restore snapshot {pin['snapshot']} "
            "from the pinned URL; do not update the pin to accept drift."
        )
    HgncResolver.from_file(path)  # Reject a pinned file without the required mapping.


def acquire(path: Path, pin: dict) -> None:
    """Reuse a verified copy, or verify a download before installing it."""
    if path.exists():
        verify(path, pin)
        log(f"  HGNC {pin['snapshot']}: verified {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    request = urllib.request.Request(
        pin["url"], headers={"User-Agent": "sagebrain-tap-opentargets-ingest"})
    try:
        with urllib.request.urlopen(request, timeout=900) as response, \
                temporary.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1 << 20)
        verify(temporary, pin)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    log(f"  HGNC {pin['snapshot']}: downloaded and verified {path}")
