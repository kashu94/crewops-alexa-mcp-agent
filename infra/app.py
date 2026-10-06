"""CDK entry point. Run it through ``scripts/cdk.sh`` from the repository root.

Settings come from ``.env`` and the environment, exactly as for the application, so the
deployed resource names always match what the code expects. The account comes from the AWS
profile named in ``.env`` (``AWS_PROFILE``); access keys stay in ``~/.aws``, never here.
"""

from __future__ import annotations

import os

from aws_cdk import App, Environment, Tags
from crewops_infra.data_stack import DataStack

from crewops.config import Settings, load_env_file


def main() -> None:
    load_env_file()
    settings = Settings.from_env()
    app = App()
    env = Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=settings.aws_region)

    DataStack(app, f"CrewOps-{settings.stage}-Data", settings=settings, env=env)

    Tags.of(app).add("project", "crewops")
    Tags.of(app).add("stage", settings.stage)
    app.synth()


if __name__ == "__main__":
    main()
