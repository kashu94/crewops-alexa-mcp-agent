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
  controller asks, and each change is pushed live to the console and, where the admin has
  set it up, emailed to the role that must act.
- **Picks up where you left off.** "Alexa, anything waiting for me?" brings back pending work
  hours later: a plan to review for a controller, a plan to sign off for an approver. Within
  a conversation, a reply such as "Captain" or "is she legal?" is read with the question
  before it.
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
        DynamoDB ──Streams──┬──► disruption analysis (Lambda)
                    ▲       └──► notifier (Lambda) ──► live console (AppSync Events)
                    │                              └─► email alerts (SNS, by role)
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
data/eval/              Gold questions, paraphrases, reference scenarios and the held-out set
data/team/              The demo team: usernames and roles, nothing private
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
  agent/                Question pipeline: router, planner, verifier, templates, Nova roles
  auth/                 Sign-in: the team, token checks, claims to caller, account sync
  plans/                Plans and approvals, with the four-eyes rules
  mcp/                  The MCP server for AgentCore, its package builder and a client
  notify/               Live console events and admin-managed email alerts
infra/                  AWS CDK app: one stack per part of the system
tests/                  Unit tests
docs/                   Architecture, decisions, friction log
```

More folders arrive as the build progresses: the web app and the Alexa skill at the top
level.

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
| `CREWOPS_GUARDRAIL_VERSION` | none | Published guardrail version; required with the id |
| `CREWOPS_MAX_TOOL_ITERATIONS` | `8` | Cap on the advisor's tool loop |
| `CREWOPS_RESPONSE_BUDGET_SECONDS` | `6.0` | Time budget for a spoken answer |
| `CREWOPS_ROUTER_SEMANTIC_FLOOR` | `0.45` | Least similarity for the router to borrow an example question's intent; set from `crewops.agent.calibrate` |
| `CREWOPS_QUEUE_SENDERS` | none | IAM ARNs, besides the MCP server, allowed to send reports to the queue (comma-separated) |

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
disruption. Only the MCP server may send reports (it sets the sender from the user's
verified login): the queue's policy refuses everyone else, unless their IAM ARN is listed in
`CREWOPS_QUEUE_SENDERS` when the ingestion stack is deployed. To keep using this development
command, which states the sender directly, list your own IAM user's ARN there; do the same
before moving messages back from the dead-letter queue, since that sends them again as you:

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

### The demo team

Sign-in uses a Cognito user pool with one group per role (the `CrewOps-<stage>-Auth` stack).
Nobody signs themselves up, and no email or phone is stored. The team is defined in
`data/team/demo_team.json`: three controllers (one per desk), three approvers, two
reporters, four crew members, an auditor, an admin and a super admin. To create or update
the accounts after deploying the stack:

```bash
uv run python -m crewops.auth.provision            # what would change
uv run python -m crewops.auth.provision --apply    # asks for the accounts' password once
```

The password is read from `CREWOPS_TEAM_PASSWORD` in the shell, or asked for, checked against
the pool's rules before anything changes, and never stored. Every demo account shares it, so
treat it as a demo password only. Running the command again changes only what differs from
the file. An account removed from the file loses its roles and profile, and `--prune`
deletes it too.

### Plans and approvals

A controller submits cover for a disruption on their desk, and a different person with the
approver role approves it, which re-checks legality and commits the roster in one step:

```bash
uv run python -m crewops.plans.desk --as rohit queue
uv run python -m crewops.plans.desk --as rohit submit r-demo-0001
uv run python -m crewops.plans.desk --as meera approve r-demo-0001 <plan id>
uv run python -m crewops.plans.desk --as meera show r-demo-0001     # plans and audit trail
uv run python -m crewops.plans.desk --as rohit close r-demo-0002 --reason "DX589 cancelled"
```

This is an operator's tool, acting as the named team member with your AWS credentials; people
reach the same rules through the assistant, signed in.

### The MCP server

The assistant is served over MCP from AgentCore Runtime, signed in with the team's accounts.
AgentCore runs code packages on Linux ARM64 and does not install dependencies, so the
package is built first (it downloads ARM64 wheels; no Docker needed), then deployed:

```bash
uv run python -m crewops.mcp.package            # builds build/mcp_server
scripts/cdk.sh deploy CrewOps-dev-Ingestion CrewOps-dev-Notify CrewOps-dev-Mcp
```

Until the package is built, the CDK app leaves the MCP stack out and says so, so the other
stacks deploy from a fresh clone. The CDK zips the package on the machine that deploys it.
If the server fails to start after a deploy from Windows (AgentCore expects Linux file
permissions, which a Windows zip may not carry), build and deploy this stack from Linux,
macOS or WSL with the repository on the Linux filesystem.

Then call it as any team member, the way an MCP host would (it asks for the password):

```bash
uv run python -m crewops.mcp.client --as rohit tools
uv run python -m crewops.mcp.client --as rohit ask "Who covers C-1042 on 15 Sep?"
uv run python -m crewops.mcp.client --as rohit my_queue
uv run python -m crewops.mcp.client --as crew1042 report_disruption \
  '{"report_type": "crew_unavailable", "details": {"crew_id": "C-1042", "from_date": "2026-09-18", "reason": "sick"}}'
```

Its tools: `ask` (the full verified answer pipeline), `my_queue`, `show_disruption`,
`report_disruption`, the plan actions (`submit_plan`, `approve_plan`, `reject_plan`,
`withdraw_plan`, `close_disruption`), `list_crew_tools` / `crew_tool` for the
deterministic tools directly, and, for admins, `alert_addresses`, `add_alert_address` and
`remove_alert_address`. What each user may do depends on their role.

A follow-up needs the same conversation id on each question. The previous turn then fills
in only what a reply giving just a seat, a date or an id, or a "he", "she" or "that
pairing", leaves out, and the answer says so in its notes:

```bash
uv run python -m crewops.mcp.client --as rohit --conversation c1 ask "Who can cover P-2291 on 15 Sep?"
uv run python -m crewops.mcp.client --as rohit --conversation c1 ask "Captain"
```

### Live updates and alerts

Every disruption status change is published to the `/ops/disruptions` channel of an AppSync
Events API, which staff (not crew or reporters) can subscribe to with their sign-in; the web
console uses it to refresh. Email alerts are optional and only an admin can set them up:
an address is subscribed to the controller or approver alerts, AWS emails it a link to
confirm, and after that it receives that role's alerts (options ready, a plan sent back, a
plan waiting for approval). An address holds one role, and at most five addresses can be
added a day.

```bash
uv run python -m crewops.mcp.client --as priya add_alert_address \
  '{"email": "you@example.com", "role": "approver"}'
uv run python -m crewops.mcp.client --as priya alert_addresses
```

Alerts and events name the disruption and its status, never a person.

### Asking a question

Answers come from the tools and are verified before they are printed. Offline, no model is
called. With `--live`, the Nova models triage, advise and reword, the guardrail screens the
question and the answer once `CREWOPS_GUARDRAIL_ID` is set, and the tokens used are printed.

```bash
uv run python -m crewops.agent.ask "Who is on reserve at BLR on 15 Sep?"
uv run python -m crewops.agent.ask --role crew --crew-id C-1042 "How many duty hours do I have?"
uv run python -m crewops.agent.ask --live "C-1042 is sick, who covers?"   # live, with models
uv run python -m crewops.agent.ask --live --no-model "C-1042 is sick, who covers?"
uv run python -m crewops.agent.calibrate      # measure the router's similarity floor (live)
```

The guardrail is its own stack. After deploying it, copy its two outputs into `.env`. A
change to the guardrail publishes a new version number and keeps the old one, so update
`CREWOPS_GUARDRAIL_VERSION` after such a deploy:

```bash
CREWOPS_GUARDRAIL_ID=<GuardrailId output>
CREWOPS_GUARDRAIL_VERSION=<GuardrailVersionNumber output>
```

Set the floor that `calibrate` recommends as `CREWOPS_ROUTER_SEMANTIC_FLOOR` in `.env`.

### Evaluation

`data/eval/heldout.json` was written after the router rules were final and is never used to
change them. It rewords each of the 38 gold questions (same ids and dates, so the two
answers can be compared) and adds behaviour cases: off-topic questions, what each role may
see, questions back, follow-ups and prompt injection. Scoring is deterministic; no model
judges another.

```bash
uv run python -m crewops.agent.evaluate                       # offline, free
uv run python -m crewops.agent.evaluate --live --json build/evals/live.json
```

`--live` asks 99 questions (each rewording with its gold question, and both turns of each
follow-up), each with several Bedrock calls, billed per token, and prints the calls and
tokens used. It reads the deployed table, so live agreement compares two live answers, not
fixed ones. Offline results, with no model and no semantic stage (rules and templates
only):

| Measure | Offline |
|---|---|
| Rewordings whose answer states at least 80% of the gold answer's facts | 25 of 38 |
| Rewordings routed to the gold question's intent | 25 of 38 |
| Mean recall of the gold answer's facts | 0.72 |
| Off-topic questions that ran no tool | 7 of 8 |
| Role, question-back, follow-up and injection cases | 12 of 12 |
| Answers failing an independent re-verification against their tool results | 0 |
| Answers showing a redaction marker (personal data reached them) | 0 |

The rewordings the rules miss are what the semantic stage and the triage model are for;
the live run measures them.

## Friction log

Problems hit while building are recorded as they happened in
[docs/FRICTION_LOG.md](docs/FRICTION_LOG.md), in four groups: the Amazon ecosystem (Alexa,
Bedrock and Nova, AgentCore, MCP), the AWS backend infrastructure, the core software stack
and local tooling, and the bugs that tests, reviews and live runs found in our own code,
with how each was fixed.

## License

[MIT](LICENSE)
