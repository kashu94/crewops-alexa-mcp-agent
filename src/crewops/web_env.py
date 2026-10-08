"""Write the web app's settings from the deployed stacks' outputs.

    uv run python -m crewops.web_env          # writes web/.env.local

The web app needs the Cognito app client, the MCP runtime and the live-events API. They
are read from the ``CrewOps-<stage>-Auth``, ``-Mcp`` and ``-Notify`` stack outputs, so
nothing is copied by hand. The file holds no secret (the app client has none) and is
ignored by git. The AWS profile named in ``.env`` is passed on for the spoken answers
(Amazon Polly), which the app's server calls with your credentials when run locally.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[2] / "web"


def lines(region: str, auth: dict, mcp: dict, notify: dict, profile: str | None) -> list[str]:
    out = [
        "# Written by `python -m crewops.web_env` from the stack outputs. Not committed.",
        f"CREWOPS_REGION={region}",
        f"CREWOPS_USER_POOL_CLIENT_ID={auth['UserPoolClientId']}",
        f"CREWOPS_MCP_RUNTIME_ARN={mcp['McpRuntimeArn']}",
        f"CREWOPS_EVENTS_HTTP_DOMAIN={notify['EventsHttpDomain']}",
        f"CREWOPS_EVENTS_REALTIME_DOMAIN={notify['EventsRealtimeDomain']}",
    ]
    if profile:
        out.append(f"AWS_PROFILE={profile}")
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=WEB_DIR / ".env.local")
    args = parser.parse_args(argv)

    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    from crewops.config import Settings, load_env_file
    from crewops.mcp.client import outputs

    load_env_file()
    profile = os.environ.get("AWS_PROFILE")
    if not profile or profile == "default":
        print("Set AWS_PROFILE (for example in .env) to a named profile.", file=sys.stderr)
        return 1
    settings = Settings.from_env()
    try:
        cloudformation = boto3.Session(region_name=settings.aws_region).client("cloudformation")
        stack = f"CrewOps-{settings.stage}"
        found = [outputs(cloudformation, f"{stack}-{part}") for part in ("Auth", "Mcp", "Notify")]
        text = "\n".join(lines(settings.aws_region, *found, profile)) + "\n"
    except (ClientError, BotoCoreError) as exc:
        print(f"AWS error: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(f"A stack output is missing: {exc}. Deploy the stacks first.", file=sys.stderr)
        return 1
    args.out.write_text(text)
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
