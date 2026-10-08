# Friction log

Problems hit while building CrewOps, across the whole stack, each written when it happened
with the real error text. Entries are grouped by where the problem was:

1. **Amazon ecosystem:** Alexa, Amazon Bedrock and Nova, AgentCore, and the Model Context
   Protocol (MCP) server and SDK.
2. **AWS backend infrastructure:** accounts, CDK, DynamoDB, SQS, Lambda, streams.
3. **Core software stack and local tooling:** Python, uv, Node, test tooling.

Entry numbers run in the order the problems happened, across all three groups.

Severity: **Blocker** (no way forward without a workaround), **High** (cost hours),
**Medium** (cost under an hour), **Low** (annoyance).

## Template

```
### FL-NNN: <short title>

- Date:
- Area: Amazon ecosystem | AWS backend infrastructure | Core software stack
- Service / tool:
- Severity:
- Task attempted:
- Steps taken:
- Expected:
- Actual: (exact error text, screenshot in docs/friction/ if useful)
- Workaround:
- Suggestion: (for Amazon, or for the tool's maintainers)
```

## 1. Amazon ecosystem (Alexa, Bedrock and Nova, AgentCore, MCP)

### FL-005: The MCP SDK's FastMCP runs plain-function tools on the event loop

- Date: 2026-10-07
- Service / tool: MCP Python SDK (`mcp` 1.30, FastMCP), hosted on AgentCore Runtime
- Severity: Medium (found in review before deployment, about an hour)
- Task attempted: Serve CrewOps' tools over streamable HTTP. The tools call DynamoDB,
  Bedrock and Cognito's key endpoint, so each one blocks for up to several seconds.
- Steps taken:
  1. Declared the tools as ordinary `def` functions, as most FastMCP examples do.
  2. Reviewed how FastMCP calls them: `func_metadata.call_fn_with_arg_validation` does
     `return fn(**arguments_parsed_dict)` for a non-async tool, directly on the event loop.
- Expected: A synchronous tool runs in a worker thread, as Starlette and FastAPI do for
  sync endpoints, or the documentation warns that it will not.
- Actual: No error and no warning. One slow question would hold every other request on
  the server until it finished.
- Workaround: Made every tool `async` and ran the blocking work with
  `anyio.to_thread.run_sync`, with locks around the state the tools share.
- Suggestion: Run sync tools in a thread pool by default, or say plainly in the FastMCP
  documentation that sync tools block the server.

### FL-006: FastMCP turns on DNS-rebinding protection silently, based on the host

- Date: 2026-10-07
- Service / tool: MCP Python SDK (`mcp` 1.30, FastMCP), streamable HTTP transport
- Severity: Low (about 15 minutes)
- Task attempted: Bind local runs of the MCP server to `127.0.0.1` (AgentCore needs
  `0.0.0.0`), and keep the in-process protocol tests working.
- Steps taken:
  1. Changed the default host from `0.0.0.0` to `127.0.0.1`.
  2. Ran the tests, which call the app through `httpx.ASGITransport` with base URL
     `http://test`.
- Expected: The bind address only decides where the server listens.
- Actual: Four tests failed with `httpx.HTTPStatusError: Client error '421 Misdirected
  Request' for url 'http://test/mcp'`. The only clue was a log line,
  `WARNING mcp.server.transport_security: Invalid Host header: test`. FastMCP turns on
  DNS-rebinding protection with a fixed host allow-list whenever the host is
  `127.0.0.1`, `localhost` or `::1`, and not for `0.0.0.0`.
- Workaround: The tests now use `http://127.0.0.1:8000`, as a real local client would.
- Suggestion: Name the allowed hosts in the 421 response body, and document that the bind
  address also switches this protection on.

### FL-007: An MCP session manager can run only once, which breaks per-test reuse

- Date: 2026-10-07
- Service / tool: MCP Python SDK (`mcp` 1.30), `StreamableHTTPSessionManager`
- Severity: Low (about 20 minutes)
- Task attempted: Exercise the real MCP protocol in-process from several tests against one
  server object.
- Steps taken:
  1. Built the FastMCP server once in a fixture and opened `server.session_manager.run()`
     in each test.
  2. Also replaced the client helper `streamablehttp_client`, now deprecated with the message
     "Use `streamable_http_client` instead.", with `streamable_http_client` and an
     explicit `httpx` client.
- Expected: A session manager can be started again after it stops, or the fixture pattern
  is documented.
- Actual: From the second test on: `RuntimeError: StreamableHTTPSessionManager .run() can
  only be called once per instance. Create a new instance if you need to run again.`
- Workaround: Build a new server for each test.
- Suggestion: Allow `run()` again after a clean shutdown, or add a testing helper that
  serves an app in-process.

### FL-009: Nova 2 Lite's reworded answer adds a number the tools never returned

- Date: 2026-10-08
- Service / tool: Amazon Bedrock, Amazon Nova 2 Lite (`global.amazon.nova-2-lite-v1:0`)
  through the Converse API
- Severity: Low (the verifier caught it; no wrong answer reached the user)
- Task attempted: First live question through the deployed MCP server: "Who covers C-1042
  on 15 Sep?", answered as Rohit (controller).
- Steps taken:
  1. The pipeline built the answer from the tools, then asked Nova 2 Lite to reword it for
     speech, keeping exactly the same facts.
  2. The verifier compared every id and number in the reworded text with the tool results.
- Expected: The rewording keeps the template's facts and adds none, as the prompt asks.
- Actual: The answer's notes read `model draft rejected: 1 unsourced claim(s) (number)`.
  The reworded text contained a number found in no tool result, so the template answer
  was given instead.
- Workaround: None needed: every model draft is verified, and the deterministic template
  is the fallback. This entry tracks how often it happens, for the evals.
- Suggestion: A Converse option to constrain a rewrite to the numbers and ids in the input
  (grounded rewording) would make the explainer usable more often.

## 2. AWS backend infrastructure (accounts, CDK, DynamoDB, SQS, Lambda)

### FL-001: Project account denies CDK outside its home region, with no hint why

- Date: 2026-10-06
- Service / tool: AWS projects (new account experience), AWS Organizations service control
  policies, AWS CDK
- Severity: Blocker (about an hour lost)
- Task attempted: First deployment of the CDK app (one DynamoDB table) from a new IAM user
  with the AdministratorAccess policy, to us-east-1, the region most AWS guides default to.
- Steps taken:
  1. Created IAM user `crewops-deployer` in the project and attached AdministratorAccess.
  2. Configured an AWS CLI profile with its access key; `aws sts get-caller-identity`
     returned the expected user.
  3. Ran `cdk bootstrap`, then `cdk diff`, then `cdk deploy --all` in us-east-1.
- Expected: Bootstrap creates the CDKToolkit stack and the deploy creates the table. If a
  region is not allowed, the error should say so.
- Actual: Every step failed with an explicit deny from a service control policy the user
  never created:
  `User: arn:aws:iam::<project-account>:user/crewops-deployer is not authorized to perform:
  cloudformation:DescribeStacks on resource: arn:aws:cloudformation:us-east-1:<project-account>:stack/CDKToolkit/*
  with an explicit deny in a service control policy: arn:aws:organizations::<management-account>:policy/<org-id>/service_control_policy/<policy-id>`
  The deploy also failed on `ssm:GetParameter` for `/cdk-bootstrap/hnb659fds/version`.
  Diagnosis took several steps:
  1. `aws organizations describe-organization` showed the project is a member account of
     an organization owned by the user's own sign-up account.
  2. Signing in again always opened the project, and the console never mentioned a home
     region or any region restriction.
  3. The AWS documentation page "Managed policies for your organization" explains that
     projects deny nearly all actions outside the project's selected region.
  4. Read-only calls confirmed it: CloudFormation and DynamoDB worked in ap-southeast-2 and
     were denied in us-east-1 and us-west-2, so the project's home region is
     ap-southeast-2 (Sydney).
- Workaround: Deploy to the project's home region. Set `AWS_REGION=ap-southeast-2`, and use
  Bedrock model IDs that work there (Nova Micro in-region, Nova 2 Lite through the `global.`
  profile). The alternative, "Activate advanced features", is irreversible, so it was not
  used. Bootstrap and deploy then succeeded in ap-southeast-2.
- Suggestion for Amazon:
  1. Show the project's home region prominently in the console and in AWS Settings, with a
     note that other regions are blocked.
  2. Make the access-denied message name the cause in plain words, such as "this project
     only allows ap-southeast-2", instead of a raw policy ARN.
  3. Have `cdk bootstrap` detect a region-restricting policy and suggest the allowed region.

### FL-002: `cdk deploy` waits forever on a dropped connection, with no output

- Date: 2026-10-06
- Service / tool: AWS CDK CLI (`npx aws-cdk@2 deploy`)
- Severity: High (about 30 minutes lost, deploy moved to another machine)
- Task attempted: Deploy the data stack right after a successful bootstrap.
- Steps taken:
  1. Ran `cdk deploy --all`. Output stopped after
     `success: Published CrewOps-dev-Data Template`.
  2. Waited several minutes with no further output and no prompt.
  3. Checked CloudFormation: no stack and no change set existed.
  4. Checked CloudTrail: the last API call was `DescribeStacks`; `CreateChangeSet` was never
     sent.
  5. Checked the process: its one HTTPS socket to an AWS endpoint was stuck in
     `FIN-WAIT-1`, retransmitting.
  6. Retried twice on the same network with the same result.
- Expected: A network failure produces a timeout and a retry or an error within a minute or
  so, and the CLI says what it is waiting for.
- Actual: The CLI waited indefinitely with no message. It looked identical to a hidden
  confirmation prompt, which cost time.
- Workaround: Ran the same deploy from another machine on a different network; it finished
  in under two minutes.
- Suggestion for Amazon: Apply request timeouts with retries to every SDK call the CLI makes.
  After 30 seconds without a response, print the operation being waited on, for example
  "waiting for CloudFormation CreateChangeSet". Add a `--timeout` option.

### FL-003: `npx aws-cdk@2` silently picks an old global CLI that cannot read the app

- Date: 2026-10-06
- Service / tool: AWS CDK CLI and aws-cdk-lib
- Severity: Medium (about 20 minutes lost)
- Task attempted: Deploy on a second machine with `npx --yes aws-cdk@2 deploy --all`.
- Steps taken:
  1. Ran the deploy and got:
     `This CDK CLI is not compatible with the CDK library used by your application. Please
     upgrade the CLI to the latest version. (Cloud assembly schema version mismatch: Maximum
     schema version supported is 45.x.x, but found 54.0.0. You need at least CLI version
     2.1144.0 to read this manifest.)`
  2. Retried; same error.
  3. `npm ls -g aws-cdk` showed an old global install, 2.1020.2, which `npx aws-cdk@2`
     reused because it satisfies the `@2` range.
- Expected: The error names the CLI version and path actually used, so the stale install is
  obvious.
- Actual: The message says to upgrade but not which binary ran, and `@2` looks like it
  should fetch the current major version.
- Workaround: `npm uninstall -g aws-cdk`, then `npx --yes aws-cdk@2.1144.0 deploy --all`.
  The project's deploy script now calls `aws-cdk@latest`.
- Suggestion for Amazon: Print the running CLI's version and install path in the mismatch
  error, and document that `npx aws-cdk@2` prefers any installed 2.x over the latest
  release.

### FL-004: boto3 table resource's client silently re-serializes transaction items

- Date: 2026-10-06
- Service / tool: AWS SDK for Python (boto3), Amazon DynamoDB TransactWriteItems
- Severity: Low (caught in testing, about 30 minutes)
- Task attempted: Write a report and its audit event in one DynamoDB transaction, using the
  client from a `Table` resource (`table.meta.client`).
- Steps taken:
  1. Built the items in DynamoDB's typed wire format (`{"S": ...}`) with `TypeSerializer`,
     as the low-level client documentation shows.
  2. Called `table.meta.client.transact_write_items(...)`.
- Expected: Either the wire-format items are accepted, or the error says the client expects
  plain Python values.
- Actual: The call failed with `TransactionCanceledException` whose cancellation reasons were
  only `[TypeError, TypeError]`. The resource's client applies boto3's automatic type
  conversion, so the already-typed items were converted a second time. Nothing in the error
  points to that.
- Workaround: Pass plain Python values to `table.meta.client` and let it convert them.
- Suggestion for Amazon: Document clearly that a resource's `meta.client` uses the
  high-level, auto-converting interface. Have it detect already-typed values (a dict whose
  only key is a DynamoDB type tag) and raise an error that says so.

### FL-008: A second queue policy silently replaces the one `enforce_ssl` created

- Date: 2026-10-07
- Service / tool: AWS CDK (`aws-cdk-lib` 2.272), Amazon SQS queue policies
- Severity: Medium (found in review before deployment, about an hour)
- Task attempted: Let only the MCP server's role send reports to the ingest queue, with a
  Deny statement added from the MCP stack.
- Steps taken:
  1. The ingestion stack creates the queue with `enforce_ssl=True`, which makes CDK add an
     `AWS::SQS::QueuePolicy` that refuses non-TLS requests.
  2. The MCP stack created a second `sqs.QueuePolicy` for the same queue with the Deny.
  3. Ran `cdk synth`: no warning, and both stacks synthesized.
- Expected: A synth-time warning that the queue already has a policy, because a queue has
  exactly one policy document.
- Actual: Two `AWS::SQS::QueuePolicy` resources for one queue. Deploying both would leave
  whichever was written last, silently dropping either the TLS rule or the sender rule.
- Workaround: Moved the Deny into the ingestion stack with `queue.add_to_resource_policy`,
  so both statements share one policy, and gave the AgentCore runtime a fixed-name
  execution role so that policy can name it without a cross-stack reference.
- Suggestion: Warn at synth when two `QueuePolicy` (or `TopicPolicy`, `BucketPolicy`)
  resources target the same resource, and point to `add_to_resource_policy`.

## 3. Core software stack and local tooling (Python, uv, Node)

### FL-010: Every CDK synth and test run prints a Node end-of-life banner

- Date: 2026-10-07
- Service / tool: AWS CDK for Python (jsii runtime), Node.js 20
- Severity: Low
- Task attempted: Run the offline test suite, which synthesizes every CDK stack, and
  inspect CDK classes from Python.
- Steps taken: Ran `uv run pytest` and `python -c "from aws_cdk import ..."`.
- Expected: Quiet output, or a single warning.
- Actual: A 14-line banner on every process that loads `aws_cdk`, starting
  `Node 20 has reached end-of-life on 2026-04-30 and will no longer be supported in new
  releases after 2026-10-29.`, mixed into test and script output.
- Workaround: `JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION=1` in the test command. Upgrade
  Node before 2026-10-29.
- Suggestion: Print the banner once per day per machine, or only from the `cdk` CLI, not
  from every Python process.
