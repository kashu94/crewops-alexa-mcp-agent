# Decisions

Short records of the choices that shape this project and what was traded away.

## D1. Python

The rules engine, agents, MCP server and Lambda functions share one language. boto3, the MCP
Python SDK, Strands Agents and the ASK SDK for Python cover every AWS and Alexa surface needed.
The web app is the one exception (D9).

## D2. DynamoDB as the operational store

| | DynamoDB (on-demand) | RDS Postgres | Aurora Serverless v2 |
|---|---|---|---|
| Latency from Lambda | Single-digit ms, no connections | Low ms, needs pooling or RDS Proxy | Low ms warm, ~15 s from zero |
| Fits Alexa's 8 s limit | Yes | Yes, if always on | Only if never at zero |
| Idle cost | $0 | Instance runs all the time | $0 at zero, else minimum capacity |
| Networking | IAM only, no VPC | VPC, security groups | Data API avoids most VPC work |
| Joins and ad-hoc SQL | No | Yes | Yes |
| Change events | Streams → Lambda | Build it yourself | Build it yourself |

The data is relational in shape, but small: 150 crew, 39 pairings, one operating week. The
rules engine loads what it needs with a few key lookups and evaluates in memory, so joins are
not needed at query time. Streams give the event-driven disruption agent for free.

**Traded away:** ad-hoc SQL. Every access pattern is designed up front.

**Not chosen:** Aurora DSQL (no foreign keys, new), DocumentDB (VPC and a running instance),
Neptune (overkill), ElastiCache or MemoryDB (caches, not a system of record).

## D3. S3 Vectors with Titan Text Embeddings V2

Semantic search covers two things: finding a rule from a paraphrased question, and matching an
unusual question to a known intent. Exact lookups by crew, pairing or rule id stay in DynamoDB.

| Option | Idle cost | Verdict |
|---|---|---|
| S3 Vectors | Cents | Chosen |
| OpenSearch Serverless | High minimum | Too expensive idle |
| Aurora + pgvector | See D2 | Only if Aurora held everything |

Embeddings are 512-dimensional: enough for short rule texts and questions, at half the storage
of 1024.

## D4. No Redis and no semantic answer cache

A cache would need a VPC and NAT for no latency gain, since DynamoDB already answers in
milliseconds and model calls take seconds. A semantic answer cache is dangerous here:
"C-1042 sick on P-2291" and "C-1024 sick on P-2219" embed almost identically, so a cached
answer could send the wrong person to the wrong aircraft. Bedrock prompt caching is used
instead; it caches the fixed prompt and tool definitions, never answers.

## D5. AgentCore Runtime with Strands for the agent and MCP server

AgentCore Runtime hosts MCP servers over Streamable HTTP, validates Cognito tokens, and bills
only for active compute. Memory and Observability come with it. Lambda runs the short,
event-triggered jobs: the SQS worker, the roster loader and the disruption agent.

**Fallback:** the MCP server is stateless between requests, so it can move to Lambda behind a
Function URL without code changes if AgentCore blocks progress.

## D6. Models: Sonnet for the advisor, Haiku for triage and narration

The rules engine does the hard reasoning; the advisor only chooses tools and drafts. Voice
needs speed. Claude Sonnet 5.5 runs the advisor loop; Claude Haiku 4.5 handles triage and the
explainer rewrite. Every model id is a setting, and the evaluation set decides any change.

**Not chosen:** fine-tuning. The verifier pins correctness regardless of the model behind it,
and prompt caching plus a deterministic planner already keep tool calls short.

## D7. Most answers need no model call to route

Ordered regex rules route the common question shapes with zero model calls, and an embedding
match catches paraphrases. The triage model is reached only when both abstain. This cuts
latency on the voice path and makes routing testable.

## D8. A deterministic verifier between the advisor and the user

Every id, number, name and rule verdict in a draft must appear in a tool result from the same
turn. A failing draft is discarded and a deterministic template is spoken instead. The
explainer's rewrite is verified again. Guardrails cannot do this job: they do not know what a
sourced number is.

## D9. Next.js web app as the main Alexa+ experience

No public path connects a self-hosted MCP server to an Alexa+ device, and the rules accept a
simulated Alexa+ experience in a web app. The web app (Next.js, TypeScript, Tailwind,
shadcn/ui on Amplify) gives the Alexa+ simulator, the controller and approver console, an
admin page and a mobile crew view. A classic Alexa custom skill, tested in the Alexa Developer Console, adds
real Alexa voice as a second channel. A standard MCP host proves the server follows the spec.

## D10. Real-time through events, not polling

Reports go to SQS (with a dead-letter queue), a worker stores them, and DynamoDB Streams wake
the disruption agent. AppSync Events pushes updates to the console over WebSockets. SNS sends
outbound alerts only. Kinesis is unnecessary at this volume.

## D11. Agents recommend; people decide, with four eyes

No agent can change the roster. A controller submits one plan, a different person with the
approver role approves it, and only approval commits every assignment in one DynamoDB
transaction. Legality is re-checked at approval, because the roster may have changed while the
plan waited. Admins configure the system but cannot operate it unless they also hold an
operational role. All of this is enforced in code in the MCP server, never by a prompt.

**Traded away:** speed on routine cover. One extra human step is accepted because a crew change
moves a real person and is costly to undo. An admin-set auto-approval threshold for low-cost
reserve callouts is a possible later addition, not part of the first build.
