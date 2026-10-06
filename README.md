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
- **Reacts to disruptions on its own.** A sick call or an incident goes onto a queue. A
  disruption agent works out the blast radius and legal, cost-ranked options, then alerts the
  controller before they ask.
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
        │ router → advisor (Claude on Bedrock) → verifier →     │
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
        DynamoDB ──Streams──► disruption agent (Lambda) ──► SNS / live console
                    ▲
        SQS ingestion (live reports) · S3 roster uploads (batch)
```

Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Why each piece was chosen:
[docs/DECISIONS.md](docs/DECISIONS.md).

## AWS services

| Service | Role |
|---|---|
| Amazon Bedrock | Claude models, Titan V2 embeddings, Guardrails |
| Amazon Bedrock AgentCore | Runtime for the agent and MCP server, Memory, Observability |
| Amazon DynamoDB | Operational data (single table) with Streams |
| Amazon S3 Vectors | Semantic search over rules and question intents |
| Amazon SQS + AWS Lambda | Real-time disruption ingestion and background agents |
| Amazon SNS | Outbound alerts to controllers and approvers |
| AWS AppSync Events | Live updates to the console |
| Amazon Cognito | Sign-in, with one group per role |
| Amazon Polly | Spoken answers in the simulator |
| AWS CDK | Infrastructure as code |

## Repository layout

```
data/seed/              Sample operating week (JSON)
src/crewops/
  config.py             Runtime settings
  domain/               Typed records shared by every layer
  ingestion/            Seed loading and validation; roster uploads and live reports later
  engine/               Legality engine: duty math, the seven rules, per-crew assessment
tests/                  Unit tests
docs/                   Architecture, decisions, friction log
```

More folders arrive as the build progresses: candidate ranking, DynamoDB access, search,
tools, agents and the MCP server inside the package, then infrastructure, the web app and
the Alexa skill at the top level.

## Getting started

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env     # optional: every setting has a default
uv run pytest
uv run ruff check .
```

## Friction log

Problems hit while building with Amazon's tools are recorded in
[docs/FRICTION_LOG.md](docs/FRICTION_LOG.md).

## License

[MIT](LICENSE)
