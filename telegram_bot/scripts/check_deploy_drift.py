#!/usr/bin/env python3
"""Compare the code running in the deployed Lambda against the local working tree.

READ-ONLY against AWS: calls only lambda:GetFunction and downloads the presigned
S3 URL it returns. This module NEVER writes to AWS.

The packaging rules below mirror telegram_bot/infra/scripts/deploy.sh, so files
that exist locally but were never deployed are reported too.

Usage:
    python -m telegram_bot.scripts.check_deploy_drift [--function-name NAME] [--region REGION]

Exits 0 when the deployed zip matches the working tree, 1 on any drift.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

REGION = "eu-central-1"
DEFAULT_FUNCTION_NAME = os.environ.get("FINANCE_LAMBDA_FUNCTION", "finance-bot-webhook")
REPO_ROOT = Path(__file__).resolve().parents[2]

# Mirrors the `zip -x` patterns in telegram_bot/infra/scripts/deploy.sh.
EXCLUDED_PREFIXES = ("telegram_bot/tests/", "telegram_bot/infra/", "telegram_bot/scripts/")
EXCLUDED_PATHS = ("telegram_bot/requirements-dev.txt",)


def is_packaged(rel_path: str) -> bool:
    """True if a repo-relative path belongs in the deploy zip."""
    if not rel_path.startswith("telegram_bot/"):
        return False
    if rel_path.startswith(EXCLUDED_PREFIXES) or rel_path in EXCLUDED_PATHS:
        return False
    parts = rel_path.split("/")
    return "__pycache__" not in parts and not rel_path.endswith(".pyc")


def expected_local_files(repo_root: Path) -> set[str]:
    """Repo-relative paths the deploy zip should contain, per the packaging rules."""
    files = set()
    for path in (repo_root / "telegram_bot").rglob("*"):
        rel_path = path.relative_to(repo_root).as_posix()
        if path.is_file() and is_packaged(rel_path):
            files.add(rel_path)
    return files


@dataclass
class DriftResult:
    identical: list[str] = field(default_factory=list)
    differs: list[str] = field(default_factory=list)
    missing_locally: list[str] = field(default_factory=list)
    never_deployed: list[str] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.differs or self.missing_locally or self.never_deployed)


def compare_zip_to_tree(zip_bytes: bytes, repo_root: Path) -> DriftResult:
    """Compare every deployed file byte-for-byte against the local working tree."""
    result = DriftResult()
    deployed: set[str] = set()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            deployed.add(name)
            local_path = repo_root / name
            if not local_path.is_file():
                result.missing_locally.append(name)
            elif local_path.read_bytes() == zf.read(name):
                result.identical.append(name)
            else:
                result.differs.append(name)

    result.never_deployed = sorted(expected_local_files(repo_root) - deployed)
    result.differs.sort()
    result.missing_locally.sort()
    result.identical.sort()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect drift between the deployed Lambda code and the working tree.")
    parser.add_argument("--function-name", default=DEFAULT_FUNCTION_NAME, help="Lambda function name to inspect")
    parser.add_argument("--region", default=REGION, help="AWS region the function lives in")
    args = parser.parse_args(argv)

    try:
        client = boto3.client("lambda", region_name=args.region)
        response = client.get_function(FunctionName=args.function_name)
    except (BotoCoreError, ClientError) as exc:
        print(f"ERROR: failed to read Lambda {args.function_name} in {args.region}: {exc}", file=sys.stderr)
        return 2

    config = response["Configuration"]
    with urllib.request.urlopen(response["Code"]["Location"], timeout=60) as code_zip:
        zip_bytes: bytes = code_zip.read()

    result = compare_zip_to_tree(zip_bytes, REPO_ROOT)

    print(f"Function:     {args.function_name} ({args.region})")
    print(f"LastModified: {config['LastModified']}")
    print(f"CodeSha256:   {config['CodeSha256']}")
    print(f"Identical:    {len(result.identical)} file(s) match the working tree")

    for name in result.differs:
        print(f"DRIFT (differs):         {name}")
    for name in result.missing_locally:
        print(f"DRIFT (missing locally): {name}")
    for name in result.never_deployed:
        print(f"DRIFT (never deployed):  {name}")

    if result.has_drift:
        total = len(result.differs) + len(result.missing_locally) + len(result.never_deployed)
        print(f"\nDRIFT DETECTED: {total} file(s) differ from the deployed code. Redeploy.")
        return 1

    print("\nNo drift: deployed code matches the working tree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
