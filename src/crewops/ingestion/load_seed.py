"""Load the seed operating week into DynamoDB.

    uv run python -m crewops.ingestion.load_seed            # validate, then write
    uv run python -m crewops.ingestion.load_seed --dry-run  # validate and count only

Settings come from ``.env`` and the environment (``AWS_PROFILE``, ``AWS_REGION``,
``CREWOPS_STAGE``), the same as the CDK app, so it writes to the table that stage deployed.
Validation errors stop the load before anything is written. Writing is idempotent: running
it again replaces the same items rather than duplicating them.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from crewops.config import Settings, load_env_file
from crewops.ingestion.seed import Severity, load_seed, validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed-dir", type=Path, default=None, help="defaults to data/seed")
    parser.add_argument("--dry-run", action="store_true", help="validate and count only")
    args = parser.parse_args(argv)

    load_env_file()
    settings = Settings.from_env()

    seed = load_seed(args.seed_dir)
    issues = validate(seed)
    for issue in issues:
        print(issue, file=sys.stderr)
    errors = [i for i in issues if i.severity is Severity.ERROR]
    if errors:
        print(f"{len(errors)} validation error(s); nothing written.", file=sys.stderr)
        return 1

    from crewops.store.ops_store import OpsStore, seed_records, table_for

    if args.dry_run:
        total = sum(1 for _ in seed_records(seed))
        print(f"Dry run: {total} items would be written to {settings.table_name}.")
        return 0

    profile = os.environ.get("AWS_PROFILE")
    if not profile or profile == "default":
        print("Set AWS_PROFILE (for example in .env) to a named profile.", file=sys.stderr)
        return 1

    print(f"Writing to {settings.table_name} in {settings.aws_region} (profile {profile})...")
    counts = OpsStore(table_for(settings)).put_seed(seed)
    for entity, n in sorted(counts.items()):
        print(f"  {entity:<17} {n}")
    print(f"Done: {sum(counts.values())} items.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
