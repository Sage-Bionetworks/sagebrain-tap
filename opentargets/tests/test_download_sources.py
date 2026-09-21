"""A dataset directory must hold the pinned parts and nothing else.

Transforms open the dataset directory, not the manifest's file list, so a part
left behind by an earlier release is ingested even though every pinned file
hashes clean. Counting rows over the directory hid that: the same inflated
total was written to the manifest and read back by ``--verify``, so it agreed
with itself. These tests pin both halves -- the count, and the extra file.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opentargets import download_sources
from opentargets.common import IngestError


def write_parquet(path: Path, rows: int) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": [f"CHEMBL{n}" for n in range(rows)]}), path)


class DatasetDirectoryTests(unittest.TestCase):
    """The layer that decides which files on disk are part of a dataset."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "drug_molecule"
        write_parquet(self.directory / "part-0.parquet", 3)
        self.pinned = ["part-0.parquet"]

    def test_spark_markers_and_hidden_files_are_not_dataset_files(self):
        """pyarrow skips these by prefix, so they are not unpinned data."""
        (self.directory / "_SUCCESS").write_bytes(b"")
        (self.directory / ".hidden").write_bytes(b"x")
        self.assertEqual(download_sources.dataset_files(self.directory),
                         ["part-0.parquet"])
        self.assertEqual(download_sources.unpinned_files(self.directory, self.pinned), [])

    def test_obsolete_part_and_interrupted_download_are_unpinned(self):
        """pyarrow reads a `.part` leftover as Parquet, so it counts as data."""
        write_parquet(self.directory / "part-stale.parquet", 9)
        write_parquet(self.directory / "part-1.parquet.part", 4)
        self.assertEqual(download_sources.unpinned_files(self.directory, self.pinned),
                         ["part-1.parquet.part", "part-stale.parquet"])

    def test_row_count_ignores_everything_that_is_not_pinned(self):
        self.assertEqual(download_sources.count_rows(self.directory, self.pinned), 3)
        write_parquet(self.directory / "part-stale.parquet", 9)
        write_parquet(self.directory / "part-1.parquet.part", 4)
        self.assertEqual(download_sources.count_rows(self.directory, self.pinned), 3)

    def test_missing_directory_has_no_files_rather_than_raising(self):
        """--verify reports absent datasets by name; it should not crash first."""
        self.assertEqual(download_sources.dataset_files(self.directory / "absent"), [])


class VerifyLocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.input_dir = self.root / "input"
        self.manifest = self.root / "26.06-sources.tsv"
        write_parquet(self.input_dir / "drug_molecule" / "part-0.parquet", 3)
        write_parquet(self.input_dir / "drug_molecule" / "part-1.parquet", 2)
        self.pin_current_state()

    def pin_current_state(self) -> None:
        """Write the manifest the downloader would write for what is on disk."""
        directory = self.input_dir / "drug_molecule"
        names = download_sources.dataset_files(directory)
        files = [{"dataset": "drug_molecule", "filename": name,
                  "bytes": (directory / name).stat().st_size,
                  "sha1": download_sources._digest(directory / name, "sha1"),
                  "sha256": download_sources._digest(directory / name, "sha256")}
                 for name in names]
        rows = {"drug_molecule": download_sources.count_rows(directory, names)}
        download_sources.write_manifest(self.manifest, "26.06", "anchor",
                                        "2026-06-23", rows, files)

    def test_clean_directory_verifies(self):
        (self.input_dir / "drug_molecule" / "_SUCCESS").write_bytes(b"")
        self.assertEqual(download_sources.verify_local(self.input_dir, self.manifest), 0)

    def test_obsolete_part_fails_verification(self):
        """The regression: every pinned file hashes clean, yet the directory
        the transforms will read holds rows from a release nobody pinned."""
        write_parquet(self.input_dir / "drug_molecule" / "part-old-release.parquet", 9)
        self.assertEqual(download_sources.verify_local(self.input_dir, self.manifest), 1)

    def test_manifest_pinned_over_a_dirty_directory_no_longer_self_agrees(self):
        """The reported failure: the stale part was already there when the
        manifest was written, so its rows went into the pinned total and the
        old directory-wide recount matched it. The extra file is now the
        check, so agreement with an inflated total cannot rescue it."""
        write_parquet(self.input_dir / "drug_molecule" / "part-old-release.parquet", 9)
        inflated = {"drug_molecule": 3 + 2 + 9}
        directory = self.input_dir / "drug_molecule"
        files = [{"dataset": "drug_molecule", "filename": name,
                  "bytes": (directory / name).stat().st_size,
                  "sha1": download_sources._digest(directory / name, "sha1"),
                  "sha256": download_sources._digest(directory / name, "sha256")}
                 for name in ("part-0.parquet", "part-1.parquet")]
        download_sources.write_manifest(self.manifest, "26.06", "anchor",
                                        "2026-06-23", inflated, files)
        # Two problems: the unpinned part, and the pinned parts' real 5 rows
        # against the 14 the dirty pin recorded.
        self.assertEqual(download_sources.verify_local(self.input_dir, self.manifest), 2)

    def test_interrupted_download_leftover_fails_verification(self):
        write_parquet(self.input_dir / "drug_molecule" / "part-2.parquet.part", 4)
        self.assertEqual(download_sources.verify_local(self.input_dir, self.manifest), 1)

    def test_missing_part_is_reported_without_counting_rows(self):
        """Row counting over named files must not raise on the absent one."""
        (self.input_dir / "drug_molecule" / "part-1.parquet").unlink()
        self.assertEqual(download_sources.verify_local(self.input_dir, self.manifest), 1)

    def test_drifted_part_is_still_caught(self):
        write_parquet(self.input_dir / "drug_molecule" / "part-1.parquet", 5)
        self.assertEqual(download_sources.verify_local(self.input_dir, self.manifest), 1)


class DownloadRefusesDirtyDirectoryTests(unittest.TestCase):
    """The bad pin is minted here: a row count taken over a dirty directory is
    what a later --verify then compares against."""

    def test_stale_part_stops_the_run_before_the_manifest_is_written(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            outdir = root / "input"
            manifest = root / "manifests" / "26.06-sources.tsv"
            write_parquet(outdir / "drug_molecule" / "part-0.parquet", 3)
            write_parquet(outdir / "drug_molecule" / "part-old-release.parquet", 9)
            sha1 = download_sources._digest(
                outdir / "drug_molecule" / "part-0.parquet", "sha1")

            argv = ["download_sources", "--outdir", str(outdir),
                    "--manifest-dir", str(manifest.parent),
                    "--datasets", "drug_molecule"]
            with patch("sys.argv", argv), \
                    patch.object(download_sources.hgnc, "read_pin", return_value={}), \
                    patch.object(download_sources.hgnc, "acquire"), \
                    patch.object(download_sources, "fetch_integrity_manifest",
                                 return_value={("drug_molecule", "part-0.parquet"): sha1}), \
                    patch.object(download_sources, "_digest", return_value=sha1), \
                    patch.object(download_sources, "upstream_metadata",
                                 return_value=("2026-06-23", "success")), \
                    patch.object(download_sources, "list_parquet_parts",
                                 return_value=["part-0.parquet"]):
                with self.assertRaisesRegex(IngestError, "part-old-release.parquet"):
                    download_sources.main()
            self.assertFalse(manifest.exists())
            self.assertFalse((outdir / "release.json").exists())


if __name__ == "__main__":
    unittest.main()
