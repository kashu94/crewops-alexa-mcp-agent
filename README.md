# CrewOps for Alexa+

A voice-first crew operations agent for airline crew control, built for Alexa+ on the open
**Model Context Protocol** and **AWS**.

A captain calls in sick at 01:30. Today a crew controller digs through rosters, duty-hour
sheets and certification records to find a legal replacement. With CrewOps they ask:

> "Alexa, Captain C-1042 is sick for P-2291. Who do I use?"

CrewOps answers with a **legally checked, cost-ranked, cited** recommendation. Every crew id,
duty hour and rupee in the answer comes from a deterministic rules engine, never from the
model's own arithmetic. A verifier checks the final wording against the tool results before
anyone hears it.

> **The model selects, sequences and narrates. It never calculates.**

## What it does

- **Answers crew-control questions by voice.** Lookups ("who is on reserve at BLR?"),
  replacements ("who covers P-2291?"), legality ("is C-2087 legal for P-2291?") and
  consequences ("both A320 captains are sick, what's the joint plan?").
- **Reacts to disruptions on its own.** A sick call or an incident goes onto a queue. The
  disruption analysis works out the blast radius and legal, cost-ranked options before the
  controller asks (the live console view is planned).
- **Remembers across sessions.** "Alexa, anything waiting for me?" brings back pending work
  hours later: a plan to review for a controller, a plan to sign off for an approver.
- **Four-eyes on every crew change.** One person reports, a controller picks a plan, a different
  person approves it, and only then is it committed in one transaction. Role checks live in
  code, never in a prompt.

## Roles

| Role | Can do |
|---|---|
| **Crew** | See own schedule, report own unavailability |
| **Reporter** | Report crew unavailability, flight delays, aircraft faults, station closures, gate changes and certification lapses |
| **Controller** | Review disruptions and options, submit one plan for approval |
| **Approver** | Approve or reject submitted plans; approval commits them |
| **Auditor** | Read-only access to every disruption, decision and audit record |
| **Admin** | Manage users and roles, rule thresholds, roster uploads |
| **Super admin** | Manage admins, system-wide settings, emergency access |

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#roles-and-approvals).

## Architecture

```
 Alexa+ simulator (web)   Alexa Developer Console skill   Any MCP host
            \                        |                       /
             \                       |                      /
              ▼                      ▼                     ▼
        ┌──────────────────────────────────────────────────────┐
        │ Assistant agent · Strands on AgentCore Runtime        │
        │ router → advisor (Nova on Bedrock) → verifier →       │
        │ explainer · Bedrock Guardrails · AgentCore Memory     │
        └──────────────────────────┬───────────────────────────┘
                                   │ MCP (Streamable HTTP)
        ┌──────────────────────────▼───────────────────────────┐
        │ CrewOps MCP server · tools + interactive cards        │
        └───────────┬───────────────────────────┬──────────────┘
                    ▼                           ▼
        Legality engine (pure Python)    S3 Vectors + Titan V2
                    │
                    ▼
        DynamoDB ──Streams──► disruption analysis (Lambda) ──► live console
                    ▲
        SQS ingestion (live reports) · S3 roster uploads (batch)
```

Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Why each piece was chosen:
[docs/DECISIONS.md](docs/DECISIONS.md).

## AWS services

| Service | Role |
|---|---|
| Amazon Bedrock | Amazon Nova models, Titan V2 embeddings, Guardrails |
| Amazon Bedrock AgentCore | Runtime for the agent and MCP server, Memory, Observability |
| Amazon DynamoDB | Operational data (single table) with Streams |
| Amazon S3 Vectors | Hybrid search over rules, example questions and past scenarios |
| Amazon SQS + AWS Lambda | Real-time disruption ingestion and background agents |
| Amazon SNS | Optional email alerts, assigned to roles by the admin |
| AWS AppSync Events | Live updates to the console |
| Amazon Cognito | Sign-in, with one group per role |
| Amazon Polly | Spoken answers in the simulator |
| AWS CDK | Infrastructure as code |

## Repository layout

```
data/seed/              Sample operating week (JSON)
data/eval/              Gold example questions and reference scenarios
src/crewops/
  config.py             Runtime settings
  domain/               Typed records shared by every layer
  ingestion/            Seed loading, roster uploads (S3 + Lambda), live reports (SQS + Lambda)
  engine/               Legality engine: duty math, the seven rules, assessment, cost ranking,
                        blast radius, delay effects, joint plans
  analysis/             Disruption analysis, its stream Lambda and the run command
  search/               Hybrid keyword + semantic search over S3 Vectors
  store/                DynamoDB key layout, item conversion, reads and writes
  tools/                The tools agents call: schemas, role checks, dispatch
infra/                  AWS CDK app: one stack per part of the system
tests/                  Unit tests
docs/                   Architecture, decisions, friction log
```

More folders arrive as the build progresses: the agents and the MCP server inside the
package, then the web app and the Alexa skill at the top level.

## Getting started

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
uv run ruff check .
```

## Deploying

Infrastructure is defined with the AWS CDK in Python and deployed to `ap-southeast-2`
(Sydney), the home region of the AWS project it runs in. You need
Node.js (22 or later recommended) and an AWS CLI profile for the target account.

```bash
aws configure --profile crewops   # once: access keys stay in ~/.aws

scripts/cdk.sh bootstrap          # once per account and region
scripts/cdk.sh diff               # what would change
scripts/cdk.sh deploy --all       # create or update the stacks
scripts/cdk.sh destroy --all      # remove a dev stage completely
```

`scripts/cdk.sh` reads `.env`, refuses to run without a named profile, and prints the
target account before calling the latest CDK CLI. The CLI must be at least as new as the
`aws-cdk-lib` the app uses; an older global install fails with a "cloud assembly schema
version mismatch". On Windows, run the same settings directly:

```powershell
$env:AWS_PROFILE="crewops"; $env:AWS_REGION="ap-southeast-2"; $env:CREWOPS_STAGE="dev"
npx --yes aws-cdk@latest deploy --all --profile crewops
```

### Settings

Settings are read from a `.env` file in the repository root (not committed) and from the
environment; a value set in the shell wins over the file. Only `AWS_PROFILE` is required.

| Variable | Default | Purpose |
|---|---|---|
| `AWS_PROFILE` | none | AWS CLI profile to deploy and run with; `default` is refused |
| `AWS_REGION` | `ap-southeast-2` | Region for every resource |
| `CREWOPS_STAGE` | `dev` | Prefix for resource names (`crewops-<stage>-...`) |
| `CREWOPS_ROUTER_MODEL_ID` | `amazon.nova-micro-v1:0` | Triage model |
| `CREWOPS_ADVISOR_MODEL_ID` | `global.amazon.nova-2-lite-v1:0` | Tool-calling advisor model |
| `CREWOPS_EXPLAINER_MODEL_ID` | `global.amazon.nova-2-lite-v1:0` | Answer rewriting model |
| `CREWOPS_EMBEDDING_MODEL_ID` | `amazon.titan-embed-text-v2:0` | Embeddings for search |
| `CREWOPS_EMBEDDING_DIMENSIONS` | `512` | 256, 512 or 1024 |
| `CREWOPS_GUARDRAIL_ID` | none | Bedrock guardrail, once deployed |
| `CREWOPS_GUARDRAIL_VERSION` | `DRAFT` | Guardrail version |
| `CREWOPS_MAX_TOOL_ITERATIONS` | `8` | Cap on the advisor's tool loop |
| `CREWOPS_RESPONSE_BUDGET_SECONDS` | `6.0` | Time budget for a spoken answer |

A minimal `.env`:

```bash
AWS_PROFILE=crewops
```

Access keys never go in `.env`; they stay in `~/.aws`.

After the first deploy, load the sample week into the table (about 1,400 items):

```bash
uv run python -m crewops.ingestion.load_seed --dry-run   # validate and count, no AWS calls
uv run python -m crewops.ingestion.load_seed             # validate, then write
```

### Uploading roster changes

Admins change the operation by uploading a JSON file to the roster bucket (its name is in the
`CrewOps-dev-Ingestion` stack outputs). The file holds any of the operation's collections in
the same shapes as the seed files, for example `{"pairings": [...]}`; the seed's
`rosters.json` and `rules.json` can be uploaded as they are.

```bash
aws s3 cp week-38.json s3://<roster-bucket>/uploads/week-38.json --profile crewops
aws s3 cp s3://<roster-bucket>/results/week-38.json.result.json - --profile crewops
```

A Lambda merges the upload over the current operation, validates the whole result, and
writes the uploaded records only if there are no errors. Records with the same id are
replaced and new ids are added; nothing is deleted. Every upload gets a report under
`results/` saying accepted or rejected, with the errors and any new warnings. Upload one
file at a time: two uploads processed at the same moment are each checked against the
operation as it was before either of them.

### Sending a live report

Reports go to the SQS ingest queue, where a worker Lambda validates them, checks the role
the sender states and every id they name, and stores each one once as a REPORTED
disruption. In the finished system only the MCP server sends reports, setting the sender
from the user's verified login, and the queue accepts messages from it alone. Until then, a
development command states the sender directly:

```bash
uv run python -m crewops.ingestion.send_report crew_unavailable \
  --user u-17 --role reporter \
  --details '{"crew_id": "C-1042", "from_date": "2026-09-15", "reason": "sick"}'
```

Report types: `crew_unavailable`, `flight_delay`, `aircraft_technical`,
`station_disruption`, `gate_change`, `certification_lapse`. Add `--dry-run` to validate
without sending. Crew may report only their own unavailability; auditors and admins may not
report.

### Search indexes

After the first deploy, embed the rules, example questions and scenarios into S3 Vectors, and
try a query:

```bash
uv run python -m crewops.search.index --dry-run   # count documents, no AWS calls
uv run python -m crewops.search.index             # embed with Titan V2 and store
uv run python -m crewops.search.index --index rules --query "how much rest does a pilot need"
```

### Disruption analysis

Each new disruption is analysed automatically from the table's change stream and stored as
OPTIONS_READY. Disruptions reported before that existed, or whose analysis was parked after
a failure, can be analysed on demand; it is safe to run repeatedly:

```bash
uv run python -m crewops.analysis.run --pending
uv run python -m crewops.analysis.run --id r-demo-0001
```

The `dev` and `test` stages are disposable: destroying them deletes their data. Any other
stage keeps its table, with point-in-time recovery and deletion protection.

## Friction log

Problems hit while building with Amazon's tools are recorded in
[docs/FRICTION_LOG.md](docs/FRICTION_LOG.md).

## License

[MIT](LICENSE)
