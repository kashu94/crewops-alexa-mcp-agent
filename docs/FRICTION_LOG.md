# Friction log

Problems hit while building with Amazon's developer tools: Alexa, Bedrock, AgentCore, and the
other AWS services this project uses. Each entry is written when the problem happens, with the
real error text.

Severity: **Blocker** (no way forward without a workaround), **High** (cost hours),
**Medium** (cost under an hour), **Low** (annoyance).

## Template

```
### FL-NNN: <short title>

- Date:
- Service / tool:
- Severity:
- Task attempted:
- Steps taken:
- Expected:
- Actual: (exact error text, screenshot in docs/friction/ if useful)
- Workaround:
- Suggestion for Amazon:
```

## Entries

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
