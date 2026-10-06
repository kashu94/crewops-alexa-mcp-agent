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
  used.
- Suggestion for Amazon:
  1. Show the project's home region prominently in the console and in AWS Settings, with a
     note that other regions are blocked.
  2. Make the access-denied message name the cause in plain words, such as "this project
     only allows ap-southeast-2", instead of a raw policy ARN.
  3. Have `cdk bootstrap` detect a region-restricting policy and suggest the allowed region.
