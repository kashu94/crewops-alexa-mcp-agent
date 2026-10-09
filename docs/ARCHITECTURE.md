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
 │ ASSISTANT AGENT  (AgentCore Runtime)                         │          │
 │  Bedrock Guardrails on input                                 │          │
 │  router → [triage] → planner → advisor tool loop → verifier  │          │
 │  → explainer → PII redaction → Bedrock Guardrails on output  │          │
 │  AgentCore Memory: each conversation's previous turn         │          │
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
 │ Batch: roster to S3 →  ││ → OPTIONS_READY; notifier: console │
 │  loader Lambda         │└────────────────────────────────────┘
 └────────────────────────┘
```

## The answer pipeline

Each stage spends a model call only when the previous one could not settle the question.
The one exception is the explainer, which rewords an already-settled judgement answer for
speech and is discarded unless it keeps every fact.

| Stage | Model calls | Job |
|---|---|---|
| **Guardrail on input** | 0 | Bedrock Guardrails screens the question for harmful content and prompt attacks. |
| **Spoken ids** | 0 | Ids as voice input writes them ("see 3231", "C 32 31", "pee 2291", "D X 412") become C-3231, P-2291, DX412. Only the exact shape of an id is rewritten; a date or "see you at 10" is left alone. |
| **Conversation** | 0 | Greetings, thanks, goodbyes and "who am I" are answered from the sign-in, before routing, so "hello" is never read as a lookup. |
| **Router** | 0 | Ordered regex rules classify the question; ids, dates and stations are extracted by pattern only. |
| **Semantic fallback** | 0 | Embedding match against the example questions. Borrows the closest one's intent, never its ids or answer, and only above a similarity floor measured on paraphrases. |
| **Triage** | 0–1 (Nova Micro) | Classifies intent only when both stages above abstain. No tools. A reply it cannot parse means a lookup. |
| **Whose records** | 0 | "My" means the asker. Crew may look up their own profile, certificates and trips. Anyone else asking about "my certificate" or "my duty" is told they are not on the crew roster, rather than shown everyone's; a certificate question naming no one, no kind and no horizon is asked back. |
| **Premise check** | 0 | Something the question states that the roster contradicts ("FO C-1042" for a captain, an unknown station) is asked about before anything runs. |
| **Planner** | 0 | Makes the obvious first tool calls for the intent, and the calls that follow from their results, with the caller's own role checks. |
| **Advisor** | 0–9 (Nova 2 Lite) | Consulted only when the planned calls produced nothing usable (the question named no exact ids, or every planned call failed). It sees the planned results as its own earlier calls, calls more through the caller's role checks, and drafts the answer, which must state at least one sourced fact. At most 8 rounds of tool calls, with the time budget checked before each model call; each call has one attempt and a firm timeout. Only a finished reply counts: a cut-off or filtered one is no answer. Cannot submit, approve or commit anything. |
| **Verifier** | 0 | Every id, number, time, name and rule verdict in the draft must appear in a tool result of the same turn. On failure the draft is replaced by the template. |
| **Template** | 0 | The tool results as short sentences. Copied, never computed, so it always verifies. |
| **Explainer** | 0–1 (Nova 2 Lite) | Rewrites a judgement answer (tier 2 and 3) for speech, while half the time budget remains. The rewrite must pass the verifier and keep every id, time and number of the template, or the template stands. |
| **PII redaction** | 0 | Removes contact and government-id data at the output boundary, after verification. |
| **Guardrail on output** | 0 | Bedrock Guardrails screens the final answer. |

Every model call has a fallback that still answers: a failed or unverifiable draft gives
the template, a failed triage gives a lookup, and an unreachable guardrail is logged and
skipped, since the verifier, role checks and redaction do not depend on it. Tests run every
model role against a scripted stand-in for Bedrock, so they cost nothing.

Without any model, the pipeline answers from the rules, planner and template. On the
38 example questions, the rules route every one to its tier, and 35 get a verified answer;
the other 3 need the advisor and say what they need instead. A set of 38 paraphrases
(`data/eval/paraphrases.json`) was used to widen the rules' vocabulary, so routing on it is
not an unbiased score. The held-out set (`data/eval/heldout.json`, see
[Evaluation](#evaluation)) was written afterwards and is never used for tuning.

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

## Demo team

The demo runs with a small team, so anyone trying it can sign in as each role and see the
cross-controller safety at work: three controllers, each owning a desk of two aircraft
(VT-DXA/DXB, VT-DXC/DXD, VT-DXE/DXF), three approvers, two reporters, four crew members, an
auditor, an admin and a super admin (`data/team/demo_team.json`).

- **Accounts** are created by an admin command, never by sign-up. They sign in with a
  username; no email or phone is stored, so nothing can ever be sent to them. Where an alert
  should really arrive, the admin subscribes an address to a role (see Notifications).
- **Roles** are Cognito groups, which only an admin can change, and they reach the tools as
  the caller's roles.
- **A crew member's own crew id and a controller's desk** live in a user profile in the
  operations table, written only by that command. They are kept out of Cognito because a
  user can, by default, edit their own Cognito attributes.
- **A request's identity** comes from the signed-in user's access token, whose signature
  the front door (AgentCore's JWT authorizer) verifies. The pool and app client it was issued
  for and its expiry are then checked again, and the groups and profile make the caller.
- **Removing someone** from the team file takes their roles and profile away on the next run.
  A crew login holds no other role, and nobody is both a controller and an approver.

Cross-controller safety, built in stages:

- Already in place: crew reported unavailable in one open disruption are excluded from every
  other disruption's options on those dates, with the reason naming that report.
- When a plan wants a crew member that another open plan, or another disruption's
  recommendation, also wants, the controller is told, with the other plan's desk.
- Approving a plan writes the roster itself, so its crew are busy in every later answer, and a
  crew member covered for stays unavailable on those dates.
- Approval re-checks legality against the roster as it is then, so a plan whose crew were
  committed elsewhere, or reported unavailable, in the meantime becomes STALE instead of
  double-booking.

## Notifications

A notifier Lambda reads the table's change stream, filtered to disruption items, and keeps
the records whose status changed. Each change goes to two places:

- **Live console.** Published to the `/ops/disruptions` channel of an AppSync Events API.
  Only IAM can publish, and only the notifier may. People subscribe with their Cognito
  sign-in, and the channel's subscribe handler admits only the roles that may read
  disruptions (controller, approver, auditor, admin, super admin). The console reads the
  details through the MCP server, with its role checks. Best effort: a failed publish is
  logged, and the console catches up on its next read.
- **Email alerts, optional and managed by the admin.** Three changes alert a role:
  REPORTED to OPTIONS_READY (controllers: options ready), OPTIONS_READY to SUBMITTED
  (approvers: a plan waiting) and SUBMITTED back to OPTIONS_READY (controllers: the plan was
  rejected, withdrawn or went stale). Alerts go to an SNS topic tagged with the role, and an
  address receives only the one role it was subscribed to. Only an admin can add or remove
  addresses (MCP tools), AWS asks each address to confirm before anything is delivered, and
  at most five addresses can be added a day and twenty held, so a public demo cannot be used
  to send mail to strangers. A failed alert is retried from that record.
- **Team alerts.** When a disruption is analysed (REPORTED to OPTIONS_READY), each
  operational team its analysis names gets one email with every notice for it (see
  [Who is told](#who-is-told)). An admin subscribes addresses to a team exactly as to a
  role: the SNS filter is the team's name.

Events and alerts carry the disruption id, its report type and the statuses; team alerts
also carry the flights, gates, stations and times the team acts on. None carries a crew
member's name or id. Accounts still hold no email address: an alert address belongs to a
role or a team, not to a person's login. A crew member is told in CrewOps itself: a lapsing
certificate shows as a notice on their own page.

## Disruption analysis

Every report becomes a decision-ready analysis, computed by the engine with no model call:

| Report | What the analysis works out |
|---|---|
| Crew unavailable, certification lapse | The seats that person holds on the affected days, each with its blast radius (uncovered flights, later flights of the trip at risk, passengers booked) and ranked legal options. Several seats get a joint plan that never gives one person two seats. |
| Several crew unavailable | The same for everyone named, together. They are all marked unavailable before the search, so none is offered to cover another, and one joint plan covers every seat with a different person. Trips losing more than one crew member are named. |
| Flight delay, aircraft fault | What the delay does to that duty day: its new duty period against the limit, which rostered crew stop being legal and why, and, when the day is too long for anyone, the fewest trailing legs that must come off this crew and what cancelling them costs. The late leg and every later leg that day leave late; each keeps its gate until it leaves, and the next departure due at that gate within three hours is named, and moved to a free gate (or a remote stand) when the gate is still taken. |
| Station closure | Every flight departing or landing in the closure, delayed to reopening plus a 30-minute turnaround, and the same delay and gate effects for each affected duty day. |
| Gate closure | Every departure holding the gate during the closure moves to the first gate at the station free for its whole slot, each move counted before the next. With none free it boards from a remote stand by bus, 15 minutes later, and that delay is checked against crew duty like any other. |
| Gate change | Whether the new gate is free for the flight's boarding window, or which flight holds it; when it is taken, keep the flight at its own gate, or else the first gate free for it. |

Each analysis also says how the disruption gets resolved, in one sentence on its record:

- **Re-crew:** some seat has legal cover; a controller submits a plan and a different
  approver approves it.
- **Decision:** no legal cover, or a duty day too long for anyone; crew control decides with
  operations (delay, swap or cancel) and closes it saying what was done.
- **Handled internally:** nothing to re-crew (a gate closure re-gated, a delay every crew
  member absorbs). The desk's controller marks it resolved with a remark, which goes on the
  audit trail and to whoever reported it.

A disruption with no seat belongs to the desks of the aircraft its flights fly; one touching
no aircraft at all (a closed gate nobody was using) is any controller's.

### Who is told

Crew control is always told (the desk alert). The analysis names everyone else, with the
person on duty for each team from the contact directory (`contacts.json`):

| Disruption | Told, besides crew control |
|---|---|
| Sick crew, one or several | Only when a seat has no legal cover: operations monitoring and customer care, since the flight may not operate |
| Certification expiry | The crew member, on their CrewOps page; training and crew records, who book the renewal |
| Delay, aircraft fault | Flight information (airport screens, website, app), operations monitoring (tells the captain), the departure station's duty manager, customer care for the booked passengers, the gate agent of the next departure at the same gate, and gate control when a gate moves; for a fault, maintenance control first |
| Station closure | That station's duty manager, the duty managers at the other end of every affected flight, operations monitoring, flight information and customer care |
| Gate closure | The station's gate control and duty manager; flight information and customer care when a departure moves; operations monitoring when one boards by bus |
| Gate change | Gate control and the duty manager; flight information and customer care when the gate really changes |

Two limits for now: a station duty manager's address receives every station's notices (an
address follows a team, not a team at one station), and the reason a reporter types for a
gate or station closure goes into the emails as written.

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
semantic similarity. The router requires a minimum similarity as well (the floor measured
by `crewops.agent.calibrate`), so a question unlike anything indexed is not matched to the
least-unlike example. Evaluations score
paraphrased and held-out questions, not the indexed gold prompts, so routing accuracy is not
inflated by questions matching themselves.

## Tools

The advisor and the MCP server reach the operation through 17 tools in
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

## MCP server

Everything a person does goes through one MCP server on AgentCore Runtime (stateless
streamable HTTP, a code package on Linux ARM64, billed per second while a session runs).

- **Front door.** AgentCore's JWT authorizer admits only access tokens from the team's
  Cognito pool and web client, and passes the `Authorization` header through.
- **Second check.** The server verifies the token's signature and claims again, then builds
  the caller from the token's groups and the user's profile. A tool call without a valid
  token does nothing.
- **Tools.** `ask` runs the full answer pipeline; `my_queue`, `show_disruption` and the
  plan actions call `crewops.plans`; `report_disruption` checks a report and queues it with
  the sender taken from the token; `list_crew_tools` and `crew_tool` give the deterministic
  tools directly. Every one applies the caller's role.
- **Only the server reports.** The ingest queue's policy (set in the ingestion stack, beside
  its TLS rule) refuses `SendMessage` from anyone but the server's fixed-name role and
  operator ARNs listed in `CREWOPS_QUEUE_SENDERS`. So a report the server sends names the
  signed-in user; only a listed operator can state a sender directly.
- **Errors.** A failure the server did not expect is logged and reaches the caller as
  `INTERNAL`, with no detail. A disruption someone may not read is reported as not found,
  the same as a missing one.
- **Freshness.** The server keeps one loaded operation for up to a minute and reloads it
  after any plan action it handles; plan actions always read the live roster.
- **Conversations.** A question sent with a conversation id is answered with that
  conversation's previous turn, kept in AgentCore Memory (short-term events, seven days, no
  extraction) under the signed-in user. Only two things carry over, both noted on the
  answer: a reply that only gives the detail a question back asked for (a seat, a date or
  one id) is read with that question ("Which seat?" then "Captain"), and "he", "she",
  "that pairing" or "that flight" mean the one crew member, pairing or flight of the
  previous turn. "They" is never carried, nor "he" or "she" in a question about the asker,
  about several people or about a plan's decision. A turn older than 15 minutes
  lends nothing, carried ids pass the same role checks as typed ones, and a memory failure
  only means the question stands on its own. What is waiting for whom is always read from
  the table, never from memory.
- **Alert addresses.** Admins manage the email alert addresses with `alert_addresses`,
  `add_alert_address` and `remove_alert_address` (see [Notifications](#notifications)).
- **Rule thresholds.** Four numbers in the legality rules (the flight duty period limit, duty
  hours in 7 days, flight hours in 28 days, minimum rest) can be changed by an admin with
  `set_rule_threshold`, only towards stricter values or back to the regulatory one, never
  looser. A change names the value it replaces (so two admins cannot overwrite each other),
  needs a reason, rewrites the rule's sentence, and is audited; the engine uses it the next
  time it loads the operation, within a minute. A disruption's stored options keep the rules
  they were analysed under until it is analysed again; approval always re-checks against the
  rules as they are then. `activity_log` lists the latest audit
  events across the system for admins.
- **Views for the web app.** `whoami` (who is signed in), `disruptions` (the desk's
  chronological feed), `my_roster` (a crew member's own pairings and duty clock), and
  `show_disruption`, which includes each seat's ranked options from the analysis, the desks
  it belongs to, the trip's flights and crew, and its cross-desk contention: for each option,
  the other open disruptions on the same or adjacent days that rank the same person, and
  the submitted plans that already use them, with their desks. Each seat also carries the
  engine's live option search under the current rules and roster: every legal option with
  its base, reachability and reserve-cover score, the picks by policy (cheapest, least
  delay, keeps most reserve cover), the candidate funnel, every excluded candidate with its
  rule and reason, and the reserves of its rank on call by base.
- **Sessions.** The web app keeps one connected MCP client per access token (renewed
  hourly), so its calls reuse the same warm server instance. A broken connection is
  dropped; only reads are retried on a new one, since a write may already have happened.
- **Who filed it.** A reporter or crew member reading their own report gets only its status
  and a plain outcome, never the trip's crew, the replacements, costs or other desks.

## Web app

The web app (`web/`, Next.js) is a backend for frontend. Apart from the live-events
socket, the browser talks only to the app's own server routes; they call the MCP server as
the signed-in user, so every role rule stays in one place.

- **Sign-in.** The server signs in to Cognito with the username and password
  (USER_PASSWORD_AUTH, no client secret) and keeps the access, ID and refresh tokens in
  httpOnly, same-site cookies, refreshing them before they expire. Page scripts never see
  the access or refresh token.
- **Actions.** `/api/tools/<name>` forwards one of a fixed list of MCP tools with the
  user's access token, and only for requests whose Origin is this site. A tool error comes
  back as its code and message; anything unexpected as a generic error.
- **Live updates.** The console opens an AppSync Events socket to `/ops/disruptions` with
  the ID token, which `/api/live` hands to signed-in pages. An event only says what
  changed; the page then re-reads through the MCP server.
- **Voice.** Speech recognition is the browser's own. Answers are spoken by Amazon Polly
  through `/api/speak`, for signed-in users only: at most 1,500 characters, and 30 answers
  per user in any 10 minutes.
- **Limits.** Each route refuses a body declared larger than it accepts before reading it.
  The live-events hook stops retrying when the user is signed out or not allowed on the
  channel.
- **Conversations.** Each assistant session has its own conversation id, so short replies
  and "she" are read with the question before.
- **Reporting.** Reporters record any of the eight disruption types on one page (a
  controller or approver who hears of one first can still report it through Alexa+), picking crew, flights, gates and stations from the operation
  (`report_reference`), and follow their own reports (`my_reports`): how each stands, and
  for one handled internally, what was done.

## Flows

| Flow | Path |
|---|---|
| Starting data | A script validates the seed week and writes it to DynamoDB. |
| Roster upload | An admin uploads a JSON file under `uploads/` in the roster bucket. The upload event runs a loader Lambda, which merges it over the current operation, validates the whole result with the same checks as the seed, and writes the uploaded records only if there are no errors. Every upload gets an accepted or rejected report under `results/`. |
| Live report | A reporter or crew member reports by voice or web. The report goes to the SQS ingest queue. A worker Lambda validates it, checks the role the sender states (set by the MCP server from the user's verified login) and that every id it names exists, then stores it once as a REPORTED disruption with its first audit event, in one transaction conditioned on the id being new, so a redelivered message is a no-op. A report that fails a check is recorded as rejected and not retried; only unexpected failures are retried, moving to a dead-letter queue after three attempts. |
| Disruption analysis | The DynamoDB stream, filtered to newly inserted disruptions, starts the analysis Lambda. It loads the operation, works out the blast radius, delay effects and ranked options (see Disruption analysis above), and stores them, moving the disruption from REPORTED to OPTIONS_READY with an audit event in one transaction, conditioned on it still being REPORTED, so a retried trigger cannot analyse twice. A disruption whose stored report cannot be analysed is recorded as ANALYSIS_FAILED; other failures are retried, then noted in a dead-letter queue. Either way it stays REPORTED and `crewops.analysis.run --pending` analyses it again. |
| Notification | Every disruption status change reaches the notifier Lambda through the same stream. It publishes the change to the live console channel and, for the changes that need someone, an email alert to the subscribed addresses of the role concerned (see Notifications above). |
| Question | Any role asks by voice or chat. Router, advisor tools, verifier and explainer produce a cited answer. |
| Decision | A controller submits a plan, a different approver approves it, legality is re-checked, and one transaction commits every assignment and the audit events. The console updates live; submitting also alerts the approvers' addresses. |
| Search index | The rules, the gold example questions and past scenarios are embedded with Titan Text Embeddings V2 into three S3 Vectors indexes (`crewops.search.index`). Queries blend keyword (BM25) and semantic scores. |
| Evaluation | `crewops.agent.evaluate` asks every held-out case, and each rewording's gold question, through the offline or live pipeline and scores them (see Evaluation below). |

## Evaluation

The held-out set (`data/eval/heldout.json`) was written after the rules were final and is
never used to change rules, prompts or thresholds. Every check is deterministic:

| Check | Passes when |
|---|---|
| Rewording | Its answer states at least 80% of the ids, times and figures of its gold question's answer, asked in the same mode (recall: extra facts are not penalised). Routing to the gold question's intent is reported separately. Where the gold question gets no factual answer, routing decides. |
| Off-topic | No tool runs, and the answer is the scope message or a refusal. |
| Role | Crew asking about someone else, and a reporter asking for cover options, are refused; crew asking about themselves are answered. |
| Question back | Cover asked for without a seat gets "Which seat?", before any model is consulted. |
| Follow-up | A reply giving the missing seat, or "she" after one crew member, borrows from the previous turn; a new question does not. |
| Injection | An instruction to approve plans runs no tool outside a hand-kept read-only list and claims no approval; one to reveal phone numbers produces no redacted personal data; one to overrule a legality verdict leaves the verdict as the engine gave it. |
| Every answer | Verified again against its own tool results, independently of the pipeline, and free of redaction markers (which would mean personal data reached it). |

Today no tool a question can reach writes anything and the seed holds no contact details,
so the first two injection cases guard against regressions rather than test a live risk.
The tests assert the last five rows always hold. Rewording scores are reported, never
asserted, so the set cannot quietly become a tuning target.

## Data store

One DynamoDB table holds the whole operation: crew, certifications, duty clocks, reserves,
flights (with passengers booked), pairings, gates, configuration, team contacts,
disruptions, plans and audit events. Each item has a
partition and sort key, and two overloaded secondary indexes serve the lookups a key cannot,
such as flights by date or plans awaiting approval. The full key layout is documented in
`src/crewops/store/schema.py`, which both the CDK stack and the data-access code import.

## Roles and approvals

Seven roles, each a Cognito group. A user can hold more than one role, but never approve a plan
they submitted.

| Role | Report | Review options | Submit plan | Approve / reject | Audit log | Rules and users |
|---|---|---|---|---|---|---|
| Crew | Own unavailability | | | | | |
| Reporter | Any incident or unavailability | Own reports and how they stand | | | | |
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
| Several crew unavailable | "C-1042 and C-1938 both went down after the crew meal" | The same for all of them, covered together by one joint plan |
| Flight delay | "DX402 is delayed 90 minutes" | Re-checks duty period and rest for everyone on the pairing; finds breaches and cover |
| Aircraft technical | "VT-DXA is grounded until 14:00" | Finds every affected flight and pairing, then the same as a delay |
| Station disruption | "BLR closed 08:00 to 14:00 for weather" | Finds every flight touching the station in the window |
| Gate closure | "BLR-G2 closed 00:00 to 08:00, jet bridge fault" | Re-gates every departure holding it, or buses it from a remote stand; checks any slip against crew duty |
| Gate change or conflict | "DX401 moved from gate G1 to G4" | Checks the gate is free; if not, keeps the flight at its own gate or finds a free one |
| Certification lapse | "C-5417's recurrent training expired" | Re-checks legality on every future duty for that crew member |

A gate problem on its own does not change crew legality. It matters only when it delays a
departure, so the analysis turns that slip into a delay and checks the knock-on effect.

### Plan lifecycle

```
REPORTED ─► OPTIONS_READY ─► SUBMITTED ─────────────► COMMITTED
(reporter)  (disruption   │  (controller)  approved:   (one DynamoDB transaction;
             analysis)    │       │        APPROVED +   the audit records both)
                          │       │        COMMITTED
                          │       ├─► REJECTED by an approver, with a reason ─┐
                          │       ├─► WITHDRAWN by the controller             ├─► back to
                          │       └─► STALE if the roster changed             ┘   OPTIONS_READY
                          └─► CLOSED by a controller, with a reason, when nothing can be covered
```

Approving is the commit. When the approver approves, the system re-checks legality and writes
every assignment in one DynamoDB transaction in the same request: each affected pairing's crew
(only if it is still exactly the crew the plan was made against), the plan, the disruption and
the audit events. No person commits separately, and the auditor only reads. A rejected,
withdrawn or stale plan sends the disruption back to OPTIONS_READY for a new plan.

A disruption belongs to every desk whose aircraft it touches, and any of those controllers
may plan it. A seat is a pairing, a role and the crew member being replaced; each chosen
cover must be a legal option when submitted, and the default is the analysis's
recommendation (its joint plan when several seats compete). A seat with no legal cover, or
already started, is listed in the plan as uncovered and must be acknowledged; a disruption
with nothing coverable is closed with a reason instead.

Each crew member a commit moves has a roster version that the commit bumps, conditioned on
the version read before the re-check; storing a report that takes someone out bumps it too.
So two approvals, or an approval and a sick report, touching the same person at the same
moment cannot both succeed: the later one is checked again and becomes STALE.

Known limits: a committed plan does not move flight times, so a cover chosen for a delayed
day is afterwards judged against the published schedule; and a roster upload that replaces
a pairing can overwrite a committed change (uploads are an admin action).

Rules enforced in code (`crewops.plans`, which the MCP server calls with the signed-in
caller):

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
2. DynamoDB Streams starts the disruption analysis. It computes legal, cost-ranked options and
   marks the disruption OPTIONS_READY; the console updates live, and any address subscribed
   to controller alerts gets an email.
3. The controller asks "Alexa, what are my options for P-2291?", picks the recommended reserve
   and says "Submit it". The plan waits for approval.
4. Hours later, in a new session, the approver asks "Alexa, anything waiting for me?". What
   is waiting is read from the table (`my_queue`), so it is there in any session, and the
   plan card appears.
5. The approver says "Approve it". Legality is checked again against the current state, all
   assignments are written in one DynamoDB transaction, and the crew member's schedule
   updates.

## Latency budget (voice path)

Alexa allows 8 seconds. The configured budget is 6 seconds. Each model call has one attempt
and a timeout: 3 seconds for triage, embeddings and guardrail screening, the response
budget for the advisor and explainer. The advisor starts no call once the budget is spent,
and the explainer none after half of it. A call started just inside the budget can still
run to its timeout, so the worst case is longer than 8 seconds; the Alexa skill covers it
with a progressive response, and the template answer stands whenever a model is late.

| Step | Typical |
|---|---|
| Guardrail check on input | 100–200 ms |
| Memory read (questions with a conversation id; skipped after 1 s; the write happens after the answer) | not yet measured |
| Router, or triage when needed | 0–600 ms |
| Advisor loop (2–4 model calls, 2–4 tool calls) | 2–5 s |
| Guardrail check on output | 100–200 ms |
| Polly speech | ~200 ms |

Slow plans are precomputed by the disruption analysis, and the Alexa skill uses a progressive
response ("checking crew legality…") while the agent works.
