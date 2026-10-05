# Architecture

## Principle

**The model selects, sequences and narrates. It never calculates.**

In crew control, a hallucinated number is a real person sent to the wrong aircraft. So every
crew id, duty hour, cost and legality verdict comes from a deterministic tool call. Models are
used only where language understanding earns its cost: classifying an unusual question,
choosing which tools to call, and turning a verified answer into speech.

## System diagram

```
 CHANNELS
 ┌──────────────────────────────┐ ┌──────────────────────────┐ ┌──────────────────────┐
 │ Alexa+ simulator (Next.js)   │ │ Alexa Developer Console  │ │ Any MCP host         │
 │ Echo Show-style, role switch │ │ custom skill (ASK SDK,   │ │ (MCP Inspector,      │
 │ Polly voice, live cards      │ │ Lambda, progressive resp)│ │ Claude, Alexa+)      │
 └──────────────┬───────────────┘ └────────────┬─────────────┘ └──────────┬───────────┘
                │ Cognito JWT (one group per role)           │            │
                ▼                                            ▼            │
 ┌─────────────────────────────────────────────────────────────┐          │
 │ ASSISTANT AGENT  (Strands · AgentCore Runtime)               │          │
 │  Bedrock Guardrails on input                                 │          │
 │  router → [triage] → planner → advisor tool loop → verifier  │          │
 │  → explainer → PII redaction → Bedrock Guardrails on output  │          │
 │  AgentCore Memory: conversation context across sessions      │          │
 └──────────────────────────────┬──────────────────────────────┘          │
                                │ MCP client                              │
                                ▼                                         ▼
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │ CREWOPS MCP SERVER  (AgentCore Runtime · MCP 2025-11-25 · Streamable HTTP)      │
 │  tools: lookup · duty_clock · check_legality · find_options · ripple · simulate │
 │         joint_plan · explain_rule · search_rules · suggest_ids                  │
 │         report_unavailability · report_incident · my_schedule                   │
 │         submit_plan (controller) · approve_plan / reject_plan (approver)        │
 │  ui://  interactive cards: plan, roster, incident                               │
 │  role checks enforced in code, never by the prompt                              │
 └──────────────┬─────────────────────────────────────────────┬───────────────────┘
                ▼                                             ▼
 ┌──────────────────────────────┐              ┌─────────────────────────────┐
 │ LEGALITY ENGINE (pure Python)│              │ S3 Vectors + Titan V2        │
 │ 7 rules · duty math · cost   │              │ rules · intent examples      │
 │ candidates · ripple · joint  │              └─────────────────────────────┘
 └──────────────┬───────────────┘
                ▼
        DynamoDB (single table) ──Streams──┐
                ▲                          ▼
 INGESTION      │          ┌────────────────────────────────────┐
 ┌──────────────┴─────────┐│ DISRUPTION AGENT (Lambda)          │
 │ Live: report → SQS     ││ runs on its own when data changes: │
 │  (+DLQ) → worker Lambda││ ripple → cost-ranked options       │
 │ Batch: roster to S3 →  ││ → OPTIONS_READY → SNS + AppSync    │
 │  loader Lambda         │└────────────────────────────────────┘
 └────────────────────────┘
```

## The answer pipeline

Each stage spends a model call only when the previous one could not settle the question.

| Stage | Model calls | Job |
|---|---|---|
| **Router** | 0 | Ordered regex rules classify the question and extract ids. |
| **Semantic fallback** | 0 | Embedding match against known question intents. Borrows the closest intent, never its ids or answer. |
| **Triage** | 0–1 (Haiku) | Classifies intent only when both stages above abstain. No tools. |
| **Planner** | 0 | Seeds the obvious first tool calls for the intent. |
| **Advisor** | 1–8 (Sonnet) | Chooses further tools and drafts the answer. Capped at 8 iterations. Cannot submit, approve or commit anything. |
| **Verifier** | 0 | Every id, number, name and rule verdict in the draft must appear in a tool result. On failure the draft is replaced by a deterministic template. |
| **Explainer** | 0–1 (Haiku) | Rewrites the verified answer for speech. Its output is verified again. |
| **PII redaction** | 0 | Removes contact and identity data at the output boundary. |

Safety therefore has independent layers: Bedrock Guardrails for harmful content and prompt
injection, the verifier for unsourced claims, PII redaction for sensitive data, and role
checks in the MCP server for authorization (see [Roles and approvals](#roles-and-approvals)).

## The legality engine

Seven rules, each returning the numbers behind its verdict rather than a bare pass or fail.
"Exceeds by 1h20m" is actionable; "illegal" is not.

| Rule | Check |
|---|---|
| FDP-01 | Flight duty period at most 13 h, minus 0.5 h per sector beyond the 2nd |
| DUTY-02 | At most 60 duty hours in any 7 consecutive calendar days |
| FLT-03 | At most 100 block hours in any 28 consecutive calendar days |
| REST-04 | At least 12 h rest before the duty and before the next rostered duty |
| QUAL-05 | Rated on the aircraft type and active |
| CERT-06 | Every certification valid on every duty date |
| BASE-07 | Called out from own base, or positioned by deadhead at a cost |

Multi-day pairings are checked day by day, with earlier days of the same pairing counted in
the windows. Candidate search ranks every legal option by cost across four strategies:
reserve callout, day-off callout, deadhead, and cancellation as the explicit last resort.

Ids that do not exist are never silently corrected. A transposed id gets a "did you mean?"
question, because acting on the wrong id dispatches the wrong person.

## Roles and approvals

Seven roles, each a Cognito group. A user can hold more than one role, but never approve a plan
they submitted.

| Role | Report | Review options | Submit plan | Approve / reject | Audit log | Rules and users |
|---|---|---|---|---|---|---|
| Crew | Own unavailability | | | | | |
| Reporter | Any incident or unavailability | Affected flights only | | | | |
| Controller | Yes | Yes | Yes | | Own desk | |
| Approver | Yes | Yes | | Yes | Yes | |
| Auditor | | Read only | | | Yes | |
| Admin | | | | | Yes | Rules, rosters, users below admin |
| Super admin | | | | | Yes | Everything, including admins |

### What can be reported

Reporters and controllers can report any of these. Crew can report only their own
unavailability. Every report goes through the same queue and wakes the disruption agent.

| Event | Example | What the disruption agent does |
|---|---|---|
| Crew unavailable | "C-1042 is sick for tomorrow" | Uncovered flights, then legal cost-ranked replacements |
| Flight delay | "DX402 is delayed 90 minutes" | Re-checks duty period and rest for everyone on the pairing; finds breaches and cover |
| Aircraft technical | "VT-DXA is grounded until 14:00" | Finds every affected flight and pairing, then the same as a delay |
| Station disruption | "BLR closed 08:00 to 14:00 for weather" | Finds every flight touching the station in the window |
| Gate change or conflict | "DX401 moved from gate G1 to G4" | Checks the gate is free; if boarding slips, treats it as a delay |
| Certification lapse | "C-5417's recurrent training expired" | Re-checks legality on every future duty for that crew member |

A gate problem on its own does not change crew legality. It matters only when it delays a
departure, so the agent turns it into a delay and checks the knock-on effect.

### Plan lifecycle

```
REPORTED ─► OPTIONS_READY ─► SUBMITTED ─► APPROVED ─► COMMITTED
(reporter)  (disruption      (controller)  (approver)  (one DynamoDB
             agent)               │            │        transaction)
                                  │            └─► REJECTED: back to the controller, with a reason
                                  ├─► WITHDRAWN by the controller
                                  └─► STALE if the roster changed before approval
```

Approving is the commit. When the approver approves, the system re-checks legality and writes
every assignment in one DynamoDB transaction in the same request. No person commits separately,
and the auditor only reads.

Rules enforced in code, in the MCP server:

- **Four eyes.** The approver must be a different person from the submitter.
- **Re-check at approval.** Legality and crew availability are evaluated again when the plan is
  approved. If anything changed since submission, the plan becomes STALE instead of committing.
- **Agents never decide.** The advisor and the disruption agent only prepare options. Submit,
  approve and reject require a signed-in person with the right role.
- **Admins configure, they do not operate.** Admin and super admin cannot submit or approve
  plans unless they also hold that role. Every role change is audited.
- **Everything is logged.** Each transition records who, when, which plan, and why.

## Demo flow

1. A crew member says "I'm sick tomorrow". The report goes through SQS and is stored.
2. DynamoDB Streams starts the disruption agent. It computes legal, cost-ranked options and
   alerts the controller through SNS and the live console.
3. The controller asks "Alexa, what are my options for P-2291?", picks the recommended reserve
   and says "Submit it". The plan waits for approval.
4. Hours later, in a new session, the approver asks "Alexa, anything waiting for me?".
   Memory restores the context and the plan card appears.
5. The approver says "Approve it". Legality is checked again against the current state, all
   assignments are written in one DynamoDB transaction, and the crew member's schedule
   updates.

## Latency budget (voice path)

Alexa allows 8 seconds. The configured budget is 6 seconds, leaving margin.

| Step | Typical |
|---|---|
| Guardrail check on input | 100–200 ms |
| Memory load (first turn of a session only) | 50–100 ms |
| Router, or triage when needed | 0–600 ms |
| Advisor loop (2–4 model calls, 2–4 tool calls) | 2–5 s |
| Guardrail check on output | 100–200 ms |
| Polly speech | ~200 ms |

Slow plans are precomputed by the disruption agent, and the Alexa skill uses a progressive
response ("checking crew legality…") while the agent works.
