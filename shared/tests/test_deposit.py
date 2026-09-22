"""Contract tests for the S3 deposit tool.

The assertions here mirror what sagebrain-infra's loader accepts.  If the loader's key
contract changes, these fail first.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from botocore.exceptions import ClientError

from shared.deposit import (
    SNAPSHOT_RE,
    WATCH_UNKNOWN_EXIT,
    collect_ttl_files,
    deposit,
    md5_hex,
    preflight,
    validate_manifest,
    validate_portal,
    validate_snapshot,
    watch_load,
)
from shared.rdf import IngestError

MANIFEST_TTL = (
    "@prefix dcterms: <http://purl.org/dc/terms/> .\n"
    "<urn:sagebrain:reactome:2026-09-21> dcterms:hasVersion \"v97\" .\n"
)


class SnapshotTokenTests(unittest.TestCase):
    def test_accepts_iso_date(self):
        self.assertEqual(validate_snapshot("2026-09-21"), "2026-09-21")

    def test_rejects_release_tag_with_actionable_message(self):
        with self.assertRaises(IngestError) as ctx:
            validate_snapshot("v97")
        self.assertIn("release tag", str(ctx.exception))
        self.assertIn("provenance", str(ctx.exception))

    def test_rejects_other_shapes(self):
        for bad in ("2026-9-21", "20260921", "latest", "2026-09-21/", ""):
            with self.subTest(bad=bad), self.assertRaises(IngestError):
                validate_snapshot(bad)

    def test_regex_matches_the_loader(self):
        # sagebrain-infra src/lambda_loader/loader.py: _DATE_RE
        self.assertEqual(SNAPSHOT_RE.pattern, r"^\d{4}-\d{2}-\d{2}$")


class PortalTests(unittest.TestCase):
    def test_accepts_slug(self):
        for good in ("nf", "als", "reactome", "amp-als", "nf2"):
            with self.subTest(good=good):
                self.assertEqual(validate_portal(good), good)

    def test_rejects_non_slug(self):
        for bad in ("NF", "nf/2026", "-nf", "nf_osi", ""):
            with self.subTest(bad=bad), self.assertRaises(IngestError):
                validate_portal(bad)


class CollectTtlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_collects_recursively(self):
        (self.root / "a.ttl").write_text("")
        (self.root / "nested").mkdir()
        (self.root / "nested" / "b.ttl").write_text("")
        self.assertEqual(
            [rel for _, rel in collect_ttl_files(self.root)], ["a.ttl", "nested/b.ttl"]
        )

    def test_rejects_non_turtle(self):
        (self.root / "a.ttl").write_text("")
        (self.root / "v97-characteristics.json").write_text("{}")
        with self.assertRaisesRegex(IngestError, "failOnError"):
            collect_ttl_files(self.root)

    def test_rejects_nested_manifest(self):
        (self.root / "a.ttl").write_text("")
        (self.root / "manifest.ttl").write_text("")
        with self.assertRaisesRegex(IngestError, "Exactly one manifest.ttl"):
            collect_ttl_files(self.root)

    def test_rejects_empty_and_missing(self):
        with self.assertRaisesRegex(IngestError, "No .ttl files"):
            collect_ttl_files(self.root)
        with self.assertRaisesRegex(IngestError, "not found"):
            collect_ttl_files(self.root / "nope")


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_requires_exact_filename(self):
        wrong = self.root / "v97-manifest.ttl"
        wrong.write_text(MANIFEST_TTL)
        with self.assertRaisesRegex(IngestError, "must be named"):
            validate_manifest(wrong)

    def test_accepts_valid_turtle(self):
        manifest = self.root / "manifest.ttl"
        manifest.write_text(MANIFEST_TTL)
        self.assertEqual(validate_manifest(manifest), manifest)

    def test_rejects_malformed_turtle(self):
        manifest = self.root / "manifest.ttl"
        manifest.write_text("this is not turtle {{{")
        with self.assertRaisesRegex(IngestError, "not valid Turtle"):
            validate_manifest(manifest)


class FakeS3:
    def __init__(self):
        self.uploaded = []

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.uploaded.append(key)


class ExplodingS3:
    def upload_file(self, *args, **kwargs):
        raise AssertionError("dry run must not upload")


class DepositOrderTests(unittest.TestCase):
    """The manifest is the sentinel: it has to land after every data file."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.data_dir = root / "rdf"
        self.data_dir.mkdir()
        (self.data_dir / "a.ttl").write_text("")
        (self.data_dir / "b.ttl").write_text("")
        self.manifest = root / "manifest.ttl"
        self.manifest.write_text(MANIFEST_TTL)
        self.ttl_files = collect_ttl_files(self.data_dir)

    def tearDown(self):
        self.temp.cleanup()

    def test_manifest_uploaded_last(self):
        s3 = FakeS3()
        deposit(s3, "bucket", "reactome/2026-09-21/", self.ttl_files, self.manifest, False)
        self.assertEqual(s3.uploaded[-1], "reactome/2026-09-21/manifest.ttl")
        self.assertEqual(
            s3.uploaded[:-1],
            ["reactome/2026-09-21/a.ttl", "reactome/2026-09-21/b.ttl"],
        )

    def test_dry_run_uploads_nothing(self):
        deposit(ExplodingS3(), "bucket", "reactome/2026-09-21/", self.ttl_files,
                self.manifest, True)


class PreflightS3:
    """Just enough S3 for preflight: a prefix listing and one manifest etag."""

    NOT_FOUND = ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")

    def __init__(self, keys=(), manifest_etag=None):
        self.keys = list(keys)
        self.manifest_etag = manifest_etag

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=None):
        return {"Contents": [{"Key": k} for k in self.keys if k.startswith(Prefix)]}

    def head_object(self, Bucket, Key):
        if self.manifest_etag is None:
            raise self.NOT_FOUND
        return {"ETag": f'"{self.manifest_etag}"'}


class PreflightTests(unittest.TestCase):
    """The destination checks are the only thing standing between a typo and a bad load."""

    PREFIX = "reactome/2026-09-21/"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manifest = Path(self.temp.name) / "manifest.ttl"
        self.manifest.write_text(MANIFEST_TTL)

    def tearDown(self):
        self.temp.cleanup()

    def check(self, s3, allow_existing=False):
        preflight(s3, "bucket", self.PREFIX, self.manifest, allow_existing)

    def test_empty_prefix_passes(self):
        self.check(PreflightS3())

    def test_rejects_existing_objects(self):
        s3 = PreflightS3(keys=[self.PREFIX + "stale.ttl"])
        with self.assertRaisesRegex(IngestError, "already contains objects"):
            self.check(s3)

    def test_allow_existing_permits_leftovers(self):
        self.check(PreflightS3(keys=[self.PREFIX + "stale.ttl"]), allow_existing=True)

    def test_rejects_identical_manifest(self):
        # An etag match means the pipeline dedupes and nothing reloads -- refuse rather
        # than upload and report success.
        s3 = PreflightS3(keys=[self.PREFIX + "manifest.ttl"],
                         manifest_etag=md5_hex(self.manifest))
        with self.assertRaisesRegex(IngestError, "would upload cleanly and load nothing"):
            self.check(s3, allow_existing=True)

    def test_changed_manifest_passes(self):
        s3 = PreflightS3(keys=[self.PREFIX + "manifest.ttl"], manifest_etag="0" * 32)
        self.check(s3, allow_existing=True)


class FakeTable:
    def __init__(self, items=(), error=None):
        self.items = list(items)
        self.error = error
        self.calls = 0

    def get_item(self, Key):
        self.calls += 1
        if self.error:
            raise self.error
        item = self.items.pop(0) if self.items else None
        return {"Item": item} if item else {}


class FakeSession:
    def __init__(self, table):
        self.table = table

    def resource(self, name):
        return mock.Mock(Table=lambda _: self.table)


class WatchExitCodeTests(unittest.TestCase):
    """--watch is used as a CI gate, so only an observed 'complete' may exit 0."""

    def watch(self, table):
        with mock.patch("shared.deposit.WATCH_POLL_INTERVAL", 0):
            return watch_load(FakeSession(table), "loads", "reactome", "2026-09-21")

    def test_complete_is_success(self):
        table = FakeTable(items=[{"status": "complete", "total_records": 10}])
        self.assertEqual(self.watch(table), 0)

    def test_error_fails(self):
        table = FakeTable(items=[{"status": "error", "error": "boom"}])
        self.assertEqual(self.watch(table), 1)

    def test_unreadable_table_is_not_success(self):
        denied = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetItem")
        table = FakeTable(error=denied)
        self.assertEqual(self.watch(table), WATCH_UNKNOWN_EXIT)
        self.assertGreater(table.calls, 1)  # retried before giving up

    def test_timeout_is_not_success(self):
        with mock.patch("shared.deposit.WATCH_TIMEOUT", 0):
            code = watch_load(FakeSession(FakeTable()), "loads", "reactome", "2026-09-21")
        self.assertEqual(code, WATCH_UNKNOWN_EXIT)


if __name__ == "__main__":
    unittest.main()
