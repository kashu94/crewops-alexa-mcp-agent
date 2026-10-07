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
 │ Polly voice, live cards      │ │ Lambda, progressive resp)│ │ VS Code, Alexa+)     │
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
 │ 7 rules · duty math · cost   │              │ rules · intents · scenarios  │
 │ candidates · ripple · joint  │              └─────────────────────────────┘
 └──────────────┬───────────────┘
                ▼
        DynamoDB (single table) ──Streams──┐
                ▲                          ▼
 INGESTION      │          ┌────────────────────────────────────┐
 ┌──────────────┴─────────┐│ DISRUPTION ANALYSIS (Lambda)       │
 │ Live: report → SQS     ││ runs on its own when data changes: │
 │  (+DLQ) → worker Lambda││ ripple → cost-ranked options       │
 │ Batch: roster to S3 →  ││ → OPTIONS_READY → live console     │
 │  loader Lambda         │└────────────────────────────────────┘
 └────────────────────────┘
```

## The answer pipeline

Each stage spends a model call only when the previous one could not settle the question.

| Stage | Model calls | Job |
|---|---|---|
| **Router** | 0 | Ordered regex rules classify the question and extract ids. |
| **Semantic fallback** | 0 | Embedding match against known question intents. Borrows the closest intent, never its ids or answer. |
| **Triage** | 0–1 (Nova Micro) | Classifies intent only when both stages above abstain. No tools. |
| **Planner** | 0 | Seeds the obvious first tool calls for the intent. |
| **Advisor** | 1–8 (Nova 2 Lite) | Chooses further tools and drafts the answer. Capped at 8 iterations. Cannot submit, approve or commit anything. |
| **Verifier** | 0 | Every id, number, name and rule verdict in the draft must appear in a tool result. On failure the draft is replaced by a deterministic template. |
| **Explainer** | 0–1 (Nova 2 Lite) | Rewrites the verified answer for speech. Its output is verified again. |
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
the windows. Duty already rostered later in the week counts too, so a cover that looks legal
today cannot quietly breach a limit on a trip the crew member already has: every window the
new duty falls into is checked, including those ending on their later rostered days. Duty
flown on the snapshot day counts even when the duty-clock history has not recorded it yet.
Rest is checked on both sides of the new duty, and between the days of the pairing itself
when a delay or positioning slip eats into the overnight.

A crew member from another base is positioned on a real same-day flight from the schedule
that has not yet departed. The first day then slips until they can report 15 minutes after
landing; later days keep their schedule, so a late first release that cuts the overnight rest
below the minimum makes the option illegal. Crew already rostered on a pairing are in
position by definition. A reserve is usable only if their on-call window covers the required
report time; otherwise they are unavailable, not a cheaper option. (The rules file phrases the
window in terms of the callout time; the reference scenarios apply it to the report time, and
so does the engine.) Thresholds come from the rule parameters in the data, so changing a
limit needs no code change.

Known simplification: the positioning flight itself is not counted as duty time.

A pairing whose first report is before the snapshot ("now") has already started and cannot
be re-crewed, so candidate search refuses it.

Candidates are everyone of the needed rank not already on the pairing. Each one either
becomes a costed option or drops out at the first stage they fail: qualified, certified, in
position, rested, within limits. That funnel, with a reason for every drop, answers "why not
C-2087?" without another calculation. The engine also precomputes every comparison an
answer might speak: the premium of the next price tier, how many options tie, and how many
times the recommendation's cost cancelling would be. Seniority and the provided risk score
are shown next to each option and never change its rank. Candidate search ranks every legal option by cost across four strategies:
reserve callout, day-off callout, deadhead, and cancellation as the explicit last resort.

Ids that do not exist are never silently corrected. A transposed id gets a "did you mean?"
question, because acting on the wrong id dispatches the wrong person.

## Demo team (planned)

The demo will run with a small team, so anyone trying it can sign in as each role and see the cross-controller
safety at work: three controllers, each owning a desk of two aircraft (VT-DXA/DXB, VT-DXC/DXD,
VT-DXE/DXF), three approvers, two reporters, a few crew members, an auditor, an admin and a super
admin. Logins will use addresses on the reserved `.example` domain, which can never receive mail,
with invitation emails switched off. Where an alert should really arrive, the admin assigns a
private address to a role.

Cross-controller safety, built in stages:

- Already in place: crew reported unavailable in one open disruption are excluded from every
  other disruption's options on those dates, with the reason naming that report.
- Planned with the approval flow: when two open disruptions want the same crew member, both
  controllers see the contention and which desk holds the other; once a plan is approved, its
  crew drop out of every other disruption's options; and approval re-checks legality, so a plan
  whose crew were committed elsewhere in the meantime becomes STALE instead of double-booking.

## Notifications (planned)

Alerts will be optional and managed by the admin, never hard-coded: on the admin's
Notifications page an email address is subscribed to a role (controllers receive "options ready", approvers
"plan waiting for approval") through an SNS topic with a per-role filter. AWS asks each address
to confirm before anything is delivered. Only the admin role can add addresses, with a daily
cap, so a public demo cannot be used to send mail to strangers.

## Disruption analysis

Every report becomes a decision-ready analysis, computed by the engine with no model call:

| Report | What the analysis works out |
|---|---|
| Crew unavailable, certification lapse | The seats that person holds on the affected days, each with its blast radius (uncovered flights, later flights of the trip at risk, passengers) and ranked legal options. Several seats get a joint plan that never gives one person two seats. |
| Flight delay, aircraft fault | What the delay does to that duty day: its new duty period against the limit, which rostered crew stop being legal and why, and, when the day is too long for anyone, the fewest trailing legs that must come off this crew and what cancelling them costs. |
| Station closure | Every flight departing or landing in the closure, delayed to reopening plus a 30-minute turnaround, and the same delay effects for each affected duty day. |
| Gate change | Whether the new gate is free for the flight's boarding window, or which flight holds it. |

A trip that has already started is reported as such rather than re-crewed. Crew taken out by
other open disruptions (reported unavailable, or a reported certificate lapse) are excluded on
the dates those reports cover, so two disruptions never recommend the same absent person. Covering only the
trailing legs of a delayed day with a fresh crew (a split duty) is not costed yet; the analysis
names the legs and the cancellation cost instead.

## Search

Three small S3 Vectors indexes, each queried with a hybrid score: semantic similarity from
Titan Text Embeddings V2, and BM25 keyword scoring over the same documents, each rescaled to
0-1 and blended half and half. If one side carries no signal (a paraphrase sharing no words
with any document), it is dropped rather than allowed to cap the score.

| Index | Documents | Used for |
|---|---|---|
| `rules` | The seven legality rules | A legality question that names no rule id |
| `intents` | The 38 gold example questions, with crew, pairing, flight, aircraft and date ids masked | The router's fallback: an unusual phrasing is matched to the closest example question, and the router maps that question to its intent. Never its ids or answer |
| `scenarios` | The six reference scenarios | Classifying what kind of disruption a free-text description is. Only the kind is returned, never that scenario's answer |

The blended score is relative within an index, so every hit also carries its absolute
semantic similarity. The router (planned) will require a minimum similarity as well, so a
question unlike anything indexed is not matched to the least-unlike example. Evaluations score
paraphrased and held-out questions, not the indexed gold prompts, so routing accuracy is not
inflated by questions matching themselves.

## Tools

The advisor and, later, the MCP server reach the operation through 17 tools in
`crewops.tools`. Each answers from the legality engine and the roster; none calls a model.

| Tool | Answers |
|---|---|
| `lookup` | Rows of any entity, with filters (equality, any-of, ranges, contains), sorting, limits and group counts. The count is always computed, never left to the model |
| `duty_clock` | One crew member's day, 7-day duty and 28-day flight hours with limits and headroom |
| `notification_brief` | The facts a callout message needs, including an acknowledge-by time from the crew member's reachability |
| `check_legality` | All seven rules for one crew member on one pairing, named by id, flight, or aircraft and date |
| `find_options` | Ranked legal cover for a seat, cancellation as the baseline, and the funnel of who was excluded and why |
| `same_pairing` | Whether two crew members, named by id or name, fly together |
| `ripple` | Blast radius of losing crew, a flight, an aircraft window or a station window |
| `simulate` | What a delay does to a duty day and its crew, and which legs to drop if nobody could fly it |
| `joint_plan` | Cheapest cover for several seats at once, never one person twice |
| `check_gate` | Gate claims, delay conflicts at a gate, occupancy and inventory |
| `explain_rule`, `search_rules` | A rule by id, or the rules closest to a paraphrased question |
| `suggest_crew_ids`, `suggest_pairing_ids`, `suggest_flight_nos` | Real ids near a wrong one, for the user to choose from |
| `list_controllers`, `controller_issue_counts` | The three controller desks, and open disruptions per desk from the live store |

Three rules hold for every tool:

- **Never guess.** An id that does not exist returns the nearest real ones, and asks for
  confirmation when it looks like a typo (C-1024 for C-1042). A flight number without a date,
  or a crew member flying several pairings, returns a question naming the choices.
- **Never raise.** `dispatch` returns an error code and a message the model can act on, so a
  failed call never ends the turn.
- **Check the caller first.** Arguments are checked against each tool's JSON Schema, then the
  caller's verified roles decide what may run. Crew may ask only about themselves. Reporters
  see gates, rules and desks. Admins see desks, issue counts and rules, not crew records.
  The model is shown only the tools its caller may use.

## Flows

| Flow | Path |
|---|---|
| Starting data | A script validates the seed week and writes it to DynamoDB. |
| Roster upload | An admin uploads a JSON file under `uploads/` in the roster bucket. The upload event runs a loader Lambda, which merges it over the current operation, validates the whole result with the same checks as the seed, and writes the uploaded records only if there are no errors. Every upload gets an accepted or rejected report under `results/`. |
| Live report | A reporter or crew member reports by voice or web. The report goes to the SQS ingest queue. A worker Lambda validates it, checks the role the sender states (set by the MCP server from the user's verified login) and that every id it names exists, then stores it once as a REPORTED disruption with its first audit event, in one transaction conditioned on the id being new, so a redelivered message is a no-op. A report that fails a check is recorded as rejected and not retried; only unexpected failures are retried, moving to a dead-letter queue after three attempts. |
| Disruption analysis | The DynamoDB stream, filtered to newly inserted disruptions, starts the analysis Lambda. It loads the operation, works out the blast radius, delay effects and ranked options (see Disruption analysis above), and stores them, moving the disruption from REPORTED to OPTIONS_READY with an audit event in one transaction, conditioned on it still being REPORTED, so a retried trigger cannot analyse twice. A disruption whose stored report cannot be analysed is recorded as ANALYSIS_FAILED; other failures are retried, then noted in a dead-letter queue. Either way it stays REPORTED and `crewops.analysis.run --pending` analyses it again. (Planned: the console updates live through AppSync Events; optional email alerts assigned by the admin.) |
| Question | Any role asks by voice or chat. Router, advisor tools, verifier and explainer produce a cited answer. |
| Decision | A controller submits a plan, a different approver approves it, legality is re-checked, and one transaction commits every assignment. The crew member is notified and the audit log updated. |
| Search index | The rules, the gold example questions and past scenarios are embedded with Titan Text Embeddings V2 into three S3 Vectors indexes (`crewops.search.index`). Queries blend keyword (BM25) and semantic scores. |
| Evaluation | Gold questions and held-out cases run against the agent and are scored. |

## Data store

One DynamoDB table holds the whole operation: crew, certifications, duty clocks, reserves,
flights, pairings, gates, configuration, disruptions, plans and audit events. Each item has a
partition and sort key, and two overloaded secondary indexes serve the lookups a key cannot,
such as flights by date or plans awaiting approval. The full key layout is documented in
`src/crewops/store/schema.py`, which both the CDK stack and the data-access code import.

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
unavailability. Every report goes through the same queue and wakes the disruption analysis.

| Event | Example | What the disruption analysis does |
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
- **Agents never decide.** The advisor and the disruption analysis only prepare options. Submit,
  approve and reject require a signed-in person with the right role.
- **Admins configure, they do not operate.** Admin and super admin cannot submit or approve
  plans unless they also hold that role. Every role change is audited.
- **Everything is logged.** Each transition records who, when, which plan, and why.

## Demo flow

1. A crew member says "I'm sick tomorrow". The report goes through SQS and is stored.
2. DynamoDB Streams starts the disruption analysis. It computes legal, cost-ranked options,
   marks the disruption OPTIONS_READY, and the controller's console updates live.
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

Slow plans are precomputed by the disruption analysis, and the Alexa skill uses a progressive
response ("checking crew legality…") while the agent works.
