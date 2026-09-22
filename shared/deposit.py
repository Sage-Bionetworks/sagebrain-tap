#!/usr/bin/env python3
"""Deposit a Turtle snapshot to S3 so SageBrain's ingestion pipeline loads it.

The pipeline (sagebrain-infra) watches the Neptune data bucket for
``{portal}/{YYYY-MM-DD}/manifest.ttl`` and bulk-loads that whole folder into
``urn:sagebrain:{portal}:{date}``. This module enforces that contract and, above all,
writes ``manifest.ttl`` LAST -- it is the completion sentinel that starts the load.

Run from the repository root:

    # Deposit a Reactome release under today's date
    python -m shared.deposit --portal reactome \
        --data-dir reactome/v97/data/rdf \
        --manifest reactome/v97/data/manifest.ttl

    # Validate and print the plan without uploading
    python -m shared.deposit --portal reactome --snapshot 2026-09-21 \
        --data-dir reactome/v97/data/rdf --manifest reactome/v97/data/manifest.ttl --dry-run

    # Upload, then follow the load to completion
    python -m shared.deposit --portal reactome --data-dir ... --manifest ... --watch

Checks that run before anything is uploaded:

* The key shape matches what the loader accepts, including its strict YYYY-MM-DD
  snapshot segment.  Release tags such as ``v97`` are rejected until the loader's date
  regex is widened (sagebrain-infra issue #61).
* Only ``*.ttl`` goes up.  Neptune reads the whole prefix as Turtle with
  failOnError=TRUE, so a single stray .json/.tsv/.md fails the entire load.
* No ``*manifest.ttl`` inside the data directory. The pipeline's EventBridge rule
  matches on suffix alone, so a nested one fires a second execution that then dies on
  key parsing.
* The target prefix is empty, unless --allow-existing. Leftovers from an earlier
  attempt would be swept into the load too.
* The manifest differs from the one already at the destination, if any. The pipeline
  skips duplicate manifests by etag, so an identical one would upload cleanly and load
  nothing -- a silent no-op is worse than a refusal.

These checks run on --dry-run too, and a dry run fails on them exactly as the real
deposit would. Only the AWS lookups themselves are skipped when credentials are absent.

Exit codes: 0 deposited (and, with --watch, loaded), 1 a check failed or the load
errored, 2 --watch could not determine the outcome.

Requires boto3 and pyoxigraph (shared/requirements.txt) and credentials for the
SageBrain account.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from shared.rdf import IngestError, log

# ── configuration ─────────────────────────────────────────────────────────────

# SageBrain lives in us-east-1.  Passed explicitly to the boto3 session so an
# AWS_REGION pointing elsewhere cannot silently redirect the lookups.
DEFAULT_REGION = "us-east-1"
DEFAULT_ENV = "prod"

# Mirrors _DATE_RE in sagebrain-infra src/lambda_loader/loader.py -- keep the two in step.
SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERSION_LIKE_RE = re.compile(r"^v[0-9]", re.IGNORECASE)
PORTAL_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

MANIFEST_NAME = "manifest.ttl"
WATCH_POLL_INTERVAL = 10  # seconds between tracking-table checks
WATCH_TIMEOUT = 1800  # seconds to follow a load before giving up
WATCH_READ_RETRIES = 3  # consecutive tracking-table read failures before giving up

# --watch exits non-zero for "the load did not succeed" AND for "we could not tell":
# a CI gate that treats a timeout or a DynamoDB permission error as success is not a gate.
WATCH_UNKNOWN_EXIT = 2


# ── local validation before any AWS call ───────────────────────────


def validate_portal(portal: str) -> str:
    if not PORTAL_RE.match(portal):
        raise IngestError(
            f"Invalid portal '{portal}'.  Expected lowercase letters, digits and hyphens, "
            "e.g. 'nf' or 'reactome' -- it becomes the first segment of the S3 key."
        )
    return portal


def validate_snapshot(snapshot: str) -> str:
    """Accept only what the loader accepts today: a YYYY-MM-DD date."""
    if SNAPSHOT_RE.match(snapshot):
        return snapshot
    if VERSION_LIKE_RE.match(snapshot):
        raise IngestError(
            f"Snapshot '{snapshot}' looks like a release tag.  The loader's date regex has not "
            "been widened yet, so this key would trip the pipeline alarm instead of loading.\n"
            "Until that ships: deposit under the ingest date and record the upstream release "
            f"('{snapshot}') as provenance inside {MANIFEST_NAME}."
        )
    raise IngestError(
        f"Snapshot '{snapshot}' is not YYYY-MM-DD.  The loader rejects any other shape."
    )


def collect_ttl_files(data_dir: Path) -> list[tuple[Path, str]]:
    """Return (path, key suffix) for every .ttl under data_dir, rejecting anything else."""
    if not data_dir.is_dir():
        raise IngestError(f"Data directory not found: {data_dir}")

    ttl_files: list[tuple[Path, str]] = []
    strays: list[str] = []
    for path in sorted(p for p in data_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(data_dir)
        if path.name.endswith(MANIFEST_NAME):
            raise IngestError(
                f"Found '{rel}' inside the data directory.  Exactly one {MANIFEST_NAME} may "
                "exist per snapshot, at the top of the prefix -- pass it with --manifest.  A "
                "nested one fires a second pipeline execution that fails on key parsing."
            )
        if path.suffix.lower() == ".ttl":
            ttl_files.append((path, rel.as_posix()))
        else:
            strays.append(rel.as_posix())

    if strays:
        listed = "\n  ".join(strays[:10])
        more = f"\n  ... and {len(strays) - 10} more" if len(strays) > 10 else ""
        raise IngestError(
            f"Non-Turtle files in the data directory would fail the bulk load (failOnError=TRUE):"
            f"\n  {listed}{more}\n"
            "Keep reports and interim files outside the directory being deposited."
        )
    if not ttl_files:
        raise IngestError(f"No .ttl files found under {data_dir}")
    return ttl_files


def validate_manifest(manifest: Path) -> Path:
    """The manifest is loaded like any other file, so malformed Turtle fails the load."""
    if not manifest.is_file():
        raise IngestError(f"Manifest not found: {manifest}")
    if manifest.name != MANIFEST_NAME:
        raise IngestError(
            f"Manifest must be named '{MANIFEST_NAME}' (got '{manifest.name}') -- the pipeline "
            "triggers on that exact suffix."
        )

    import pyoxigraph

    try:
        for _ in pyoxigraph.parse(path=str(manifest), format=pyoxigraph.RdfFormat.TURTLE):
            pass
    except (SyntaxError, ValueError, OSError) as exc:
        raise IngestError(f"{manifest} is not valid Turtle: {exc}")
    return manifest


# ── AWS ───────────────────────────────────────────────────────────────────────


def aws_errors() -> tuple:
    """botocore's error bases, imported lazily like the rest of the AWS layer.

    Empty when botocore is absent: nothing could have raised one, so nothing is caught.
    """
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        return ()
    return (BotoCoreError, ClientError)


def make_session(profile: str | None, region: str):
    import boto3

    return boto3.Session(profile_name=profile, region_name=region)


def resolve_bucket(session, stack_name: str) -> str:
    """Read the data bucket name from the neptune stack's CloudFormation outputs."""
    from botocore.exceptions import ClientError

    try:
        stacks = session.client("cloudformation").describe_stacks(StackName=stack_name)["Stacks"]
    except ClientError as exc:
        raise IngestError(
            f"Could not read stack '{stack_name}': {exc}.  Pass --bucket to skip this lookup."
        )
    for output in stacks[0].get("Outputs", []):
        if output["OutputKey"] == "NeptuneDataBucketName":
            return output["OutputValue"]
    raise IngestError(f"Stack '{stack_name}' has no NeptuneDataBucketName output")


def md5_hex(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remote_etag(s3, bucket: str, key: str) -> str | None:
    """Return the object's etag, or None when it does not exist."""
    from botocore.exceptions import ClientError

    try:
        return s3.head_object(Bucket=bucket, Key=key)["ETag"].strip('"')
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def preflight(s3, bucket: str, prefix: str, manifest: Path, allow_existing: bool) -> None:
    """Inspect the destination before writing.  Raises on a blocking condition."""
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=25)
    existing = [obj["Key"] for obj in resp.get("Contents", [])]

    if existing and not allow_existing:
        listed = "\n  ".join(existing[:10])
        raise IngestError(
            f"s3://{bucket}/{prefix} already contains objects:\n  {listed}\n"
            "Those would be loaded alongside the new files.  Choose another snapshot date, "
            "clear the prefix, or pass --allow-existing if that is intended."
        )

    manifest_key = f"{prefix}{MANIFEST_NAME}"
    if remote_etag(s3, bucket, manifest_key) == md5_hex(manifest):
        raise IngestError(
            f"s3://{bucket}/{manifest_key} is already present with identical content.  The "
            "pipeline skips duplicate manifests by etag, so this deposit would upload cleanly "
            "and load nothing.\n"
            "To force a reload, deposit under a new snapshot date; to re-record the same "
            f"snapshot, change {MANIFEST_NAME} (its provenance or timestamp) so the etag moves."
        )


def watch_load(session, table_name: str, portal: str, snapshot: str) -> int:
    """Poll the pipeline's tracking table until the load settles.  Returns an exit code."""
    from botocore.exceptions import BotoCoreError, ClientError

    table = session.resource("dynamodb").Table(table_name)
    deadline = time.time() + WATCH_TIMEOUT
    last_status = None
    read_failures = 0

    log(f"\nWatching {table_name} for {portal}/{snapshot} (Ctrl-C to stop)...")
    while time.time() < deadline:
        try:
            item = table.get_item(Key={"portal": portal, "snapshot": snapshot}).get("Item")
        except (ClientError, BotoCoreError) as exc:
            # Throttling is transient; a permission error is not.  Retry a few times,
            # then report the outcome as unknown rather than as a successful load.
            read_failures += 1
            log(f"  could not read tracking table ({read_failures}): {exc}")
            if read_failures >= WATCH_READ_RETRIES:
                log("  giving up on the tracking table; the load status is unknown.")
                return WATCH_UNKNOWN_EXIT
            time.sleep(WATCH_POLL_INTERVAL)
            continue
        read_failures = 0

        status = item.get("status") if item else None
        if status != last_status:
            log(f"  status : {status or 'no row yet'}")
            last_status = status

        if status == "complete":
            log(
                f"  graph  : {item.get('named_graph')}\n"
                f"  records: {item.get('total_records')}  "
                f"parse_errors: {item.get('parsing_errors')}"
            )
            return 0
        if status == "error":
            log(f"  FAILED : {item.get('error')}")
            return 1
        time.sleep(WATCH_POLL_INTERVAL)

    log(f"  gave up after {WATCH_TIMEOUT}s; the load may still be running.")
    return WATCH_UNKNOWN_EXIT


# ── upload ────────────────────────────────────────────────────────────────────


def deposit(s3, bucket: str, prefix: str, ttl_files: list, manifest: Path, dry_run: bool) -> None:
    """Upload the data files first, then the manifest -- the order is the whole point."""
    tag = "[dry-run] " if dry_run else ""
    for path, rel_key in ttl_files:
        key = f"{prefix}{rel_key}"
        log(f"  {tag}put {key}  ({path.stat().st_size / 1024:,.0f} KB)")
        if not dry_run:
            s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": "text/turtle"})

    manifest_key = f"{prefix}{MANIFEST_NAME}"
    log(f"\n  {tag}put {manifest_key}   <- sentinel, starts the load")
    if not dry_run:
        s3.upload_file(str(manifest), bucket, manifest_key, ExtraArgs={"ContentType": "text/turtle"})


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deposit a Turtle snapshot to S3 for SageBrain's ingestion pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--portal", required=True, help="Portal / source name, e.g. nf or reactome")
    parser.add_argument("--snapshot", default=date.today().isoformat(),
                        help="Snapshot date YYYY-MM-DD (default: today)")
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="Directory of .ttl files to upload, searched recursively")
    parser.add_argument("--manifest", type=Path, required=True,
                        help=f"Path to the {MANIFEST_NAME} sentinel, uploaded last")
    parser.add_argument("--bucket", default=os.environ.get("NEPTUNE_BUCKET"),
                        help="S3 data bucket (default: NeptuneDataBucketName from the stack)")
    parser.add_argument("--env", default=DEFAULT_ENV,
                        help=f"SageBrain CDK environment, for stack names (default: {DEFAULT_ENV})")
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"), help="AWS profile")
    parser.add_argument("--region", default=DEFAULT_REGION,
                        help=f"AWS region (default: {DEFAULT_REGION})")
    parser.add_argument("--allow-existing", action="store_true",
                        help="Deposit even if the target prefix already holds objects")
    parser.add_argument("--watch", action="store_true",
                        help="Follow the load in the tracking table until it settles")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate and print the plan without uploading")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    env_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if env_region and env_region != args.region:
        log(f"note: AWS_REGION={env_region} in the environment; using {args.region} "
            "(pass --region to override)")

    session = None
    bucket = args.bucket
    s3 = None

    try:
        portal = validate_portal(args.portal)
        snapshot = validate_snapshot(args.snapshot)
        ttl_files = collect_ttl_files(args.data_dir)
        manifest = validate_manifest(args.manifest)
        prefix = f"{portal}/{snapshot}/"

        try:
            session = make_session(args.profile, args.region)
            bucket = bucket or resolve_bucket(session, f"app-{args.env}-neptune")
            s3 = session.client("s3")
        except Exception as exc:
            # A dry run stays useful without credentials: it still validates the payload.
            if not args.dry_run:
                raise
            log(f"note: cannot reach AWS, skipping destination checks ({exc})")
            bucket = bucket or "<bucket>"

        if s3 is not None:
            try:
                preflight(s3, bucket, prefix, manifest, args.allow_existing)
            except aws_errors() as exc:
                # Same bargain as above: an unreadable destination is not a verdict.
                if not args.dry_run:
                    raise
                log(f"note: could not inspect the destination ({exc})")
            # A blocking condition preflight *did* see is never downgraded to a note:
            # a green dry run for a deposit that aborts is the failure mode to avoid.

        total_mb = sum(p.stat().st_size for p, _ in ttl_files) / 1024 / 1024
        log(f"Depositing to s3://{bucket}/{prefix}")
        log(f"  graph  : urn:sagebrain:{portal}:{snapshot}")
        log(f"  files  : {len(ttl_files)} .ttl ({total_mb:,.1f} MB) + {MANIFEST_NAME}")
        log(f"  started: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")

        deposit(s3, bucket, prefix, ttl_files, manifest, args.dry_run)
    except IngestError as exc:
        log(f"\nError: {exc}")
        return 1
    except aws_errors() as exc:
        # Credentials, endpoint, AccessDenied: an operator error, not a bug -- no traceback.
        log(f"\nAWS error: {exc}")
        return 1

    if args.dry_run:
        log("\nDry run -- nothing uploaded.")
        return 0

    log(f"\nDeposited.  The pipeline picks up {prefix}{MANIFEST_NAME} within seconds.")
    if args.watch:
        return watch_load(session, f"app-{args.env}-neptune-pipeline-loads", portal, snapshot)

    key_json = f'{{"portal": {{"S": "{portal}"}}, "snapshot": {{"S": "{snapshot}"}}}}'
    log("Check progress with:\n"
        f"  aws --profile {args.profile or '<profile>'} --region {args.region} dynamodb get-item \\\n"
        f"    --table-name app-{args.env}-neptune-pipeline-loads \\\n"
        f"    --key '{key_json}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
