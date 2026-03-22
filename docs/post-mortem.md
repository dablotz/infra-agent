# Post-Mortem: Infra-Agent Development Issues

## Overview

Three distinct issues were encountered during initial deployment and testing of the infra-agent. Each is documented below with root cause analysis and the corrective action taken.

---

## Issue 1: Bedrock 403 Access Denied — Inference Profile ARN Format

**Date:** 2026-03-13
**Severity:** High — agent completely non-functional

### What Happened

After tightening the Bedrock IAM policy from `Resource: "*"` to specific model ARNs, all agent invocations returned a 403 error:

```
'failureCode': 403,
'failureReason': 'Access denied when calling Bedrock. Check your request permissions and retry the request.'
```

### Root Cause Analysis

Two separate ARN construction bugs were present:

**Bug 1 — Wrong ARN type for inference profile.**
The IAM policy used `::` (no account ID) for the inference profile:
```
arn:aws:bedrock:us-east-1::inference-profile/us.anthropic.claude-sonnet-4-5-...
```

CloudTrail showed Bedrock was actually calling:
```
arn:aws:bedrock:us-east-1:{account_id}:inference-profile/us.anthropic.claude-sonnet-4-5-...
```

Cross-region inference profiles (identifiable by the `us.`/`eu.`/`ap.` prefix on the model ID) are account-scoped resources provisioned into each AWS account. They use the account ID in their ARN, unlike foundation models which are AWS-managed and use `::`.

**Bug 2 — Cross-region prefix included in foundation model ARN.**
The `bedrock_model_id` variable holds `us.anthropic.claude-sonnet-4-5-20250929-v1:0`. When used directly in a foundation model ARN:
```
arn:aws:bedrock:us-east-1::foundation-model/us.anthropic.claude-sonnet-4-5-20250929-v1:0
```
The `us.` prefix is not part of the foundation model ID. The base model ID is `anthropic.claude-sonnet-4-5-20250929-v1:0`.

Additionally, cross-region inference profiles route requests to multiple regions (`us-east-1`, `us-west-2`, `us-east-2`), so foundation model ARNs must cover all three regions.

### Fix

Added a `locals` block in `main.tf` to derive the base model ID:
```hcl
locals {
  bedrock_base_model_id = replace(var.bedrock_model_id, "/^[a-z]{2}\\./", "")
}
```

Updated IAM resource lists:
```hcl
Resource = [
  "arn:aws:bedrock:${region}:${account_id}:inference-profile/${var.bedrock_model_id}",
  "arn:aws:bedrock:us-east-1::foundation-model/${local.bedrock_base_model_id}",
  "arn:aws:bedrock:us-west-2::foundation-model/${local.bedrock_base_model_id}",
  "arn:aws:bedrock:us-east-2::foundation-model/${local.bedrock_base_model_id}"
]
```

### Lesson Learned

**Use CloudTrail first.** The Bedrock agent trace shows a 403 but not the offending ARN. CloudTrail's `Converse`/`InvokeModel` event shows the exact resource ARN that was denied — this immediately identifies the mismatch without guesswork.

**Bedrock ARN formats are not uniform:**
- Foundation models: `arn:aws:bedrock:{region}::foundation-model/{model_id}` (no account ID)
- Cross-region inference profiles: `arn:aws:bedrock:{region}:{account_id}:inference-profile/{profile_id}` (requires account ID)
- Application inference profiles: same account-scoped format but a different resource type

Always verify the ARN format against the CloudTrail event rather than documentation alone.

---

## Issue 2: Agent Infinite Loop — Async Pipeline vs. Synchronous Expectations

**Date:** 2026-03-17
**Severity:** High — agent appeared to work but never produced output

### What Happened

After fixing the 403 error, the agent accepted requests but the session would hang indefinitely. Digging through the orchestration trace revealed the agent was generating an internal question to the user and waiting for a response that never came. The Bedrock test console showed no message to the user and the session eventually timed out.

### Root Cause Analysis

The action group handler fired a Step Functions execution asynchronously and returned immediately:

```python
response = sfn.start_execution(...)
return {"message": "IaC generation pipeline started", "execution_arn": ...}
```

The agent instructions said:
> "Provide the user with the S3 location of their generated infrastructure code"

But the action group response contained no S3 location — only an execution ARN. The agent, unable to fulfill step 4 of its instructions, entered a reasoning loop asking the user for clarification about what S3 bucket to use. That question was surfaced to the agent orchestration layer but never forwarded to the chat interface, so no response ever arrived and the agent waited indefinitely.

The root issue was a mismatch between what the instructions promised (a synchronous result with an S3 location) and what the action group actually returned (an async acknowledgment).

### Fix

**Option considered but rejected:** Changing the instructions to say "inform the user the pipeline has started." This fixes the loop but provides a poor user experience — the user has no way to know when the code is ready.

**Option chosen:** Make the action group handler synchronous. It now polls `describe_execution` every 5 seconds until the pipeline completes, then returns the actual S3 URI, validation status, and security status.

> **Superseded:** This synchronous polling approach was later replaced by a discrete action group architecture that eliminates Step Functions entirely. See *Architecture Decision: Agent-Native Pipeline* below.

```python
while True:
    status_response = sfn.describe_execution(executionArn=execution_arn)
    status = status_response["status"]
    if status == "SUCCEEDED":
        output = json.loads(status_response["output"])
        return { ..., "s3_uri": output["s3_uri"], ... }
    if status in ("FAILED", "TIMED_OUT", "ABORTED"):
        return _error_response(...)
    if context.get_remaining_time_in_millis() < 15000:
        return _error_response(...)
    time.sleep(5)
```

Supporting changes:
- Lambda timeout increased from 60s to 600s to accommodate the full pipeline
- `states:DescribeExecution` added to the action group handler IAM policy (on the execution ARN resource, not the state machine ARN)
- Agent instructions updated to say the action returns the result synchronously
- Action group OpenAPI schema updated to reflect the actual 200 response fields

### Lesson Learned

**Agent instructions must match the action group contract exactly.** The agent uses its instructions to decide what to do after calling an action. If the instructions describe a result that the action doesn't return, the agent will attempt to fill the gap — usually by asking the user a question.

**Async patterns require explicit design for agent orchestration.** A fire-and-forget action group works fine if the instructions acknowledge that the pipeline runs asynchronously and the user should check elsewhere for results. But if the instructions promise a final result, the action must deliver it synchronously.

**IAM for polling:** `states:StartExecution` operates on the state machine ARN. `states:DescribeExecution` operates on execution ARNs (a different resource format). They require separate IAM statements with different resource patterns.

---

## Issue 3: `user_request is required` — Wrong Event Structure for OpenAPI `requestBody`

**Date:** 2026-03-17
**Severity:** High — every agent request failed immediately

### What Happened

After deploying the synchronous action group handler, every request returned an error within milliseconds:

```json
{"error": "user_request is required"}
```

The execution time (32ms) confirmed the handler was rejecting the request before ever starting the pipeline. The `user_request` parameter was clearly being sent by the agent (visible in the orchestration trace's `modelInvocationInput`) but was not being extracted by the handler.

### Root Cause Analysis

The handler extracted parameters from the `parameters` field of the Lambda event:

```python
parameters = event.get("parameters", [])
user_request = next((p["value"] for p in parameters if p["name"] == "user_request"), "")
```

However, the OpenAPI schema for the action group used a `requestBody` definition rather than path or query parameters. Bedrock sends these differently:

**Path/query parameters** arrive at:
```python
event["parameters"]  # list of {name, type, value}
```

**`requestBody` properties** arrive at:
```python
event["requestBody"]["content"]["application/json"]["properties"]  # list of {name, type, value}
```

The `parameters` key was `None`/absent in the event, so `user_request` defaulted to `""`, triggering the 400 validation.

### Fix

Updated the handler to read from the correct path with a fallback:

```python
properties = (
    event.get("requestBody", {})
    .get("content", {})
    .get("application/json", {})
    .get("properties", [])
)
parameters = properties or event.get("parameters", [])
```

### Lesson Learned

**Bedrock agent event structure depends on the OpenAPI schema type.** The Lambda event format is not uniform — it changes based on whether the schema uses query/path parameters or a request body. Test the action group by logging the raw event on first deployment to confirm the structure before writing extraction logic.

**Use the test console trace's `actionGroupInvocationInput` to see the exact event** sent to Lambda. This shows both the parsed parameters and the raw event shape, making it immediately obvious when extraction is looking at the wrong key.

---

## Summary

| Issue | Root Cause Category | Time to Diagnose | Key Tool |
|---|---|---|---|
| Bedrock 403 | AWS IAM / ARN format mismatch | ~1 hour | CloudTrail |
| Agent infinite loop | Architecture / async vs sync mismatch | ~30 min | Bedrock orchestration trace |
| `user_request` 400 | AWS SDK event contract misunderstanding | ~15 min | Bedrock orchestration trace |

All three issues were invisible at deploy time and only surfaced during runtime testing. The common thread: **the Bedrock orchestration trace identifies the symptom but not the root cause — always go one layer deeper** (CloudTrail for IAM, the raw Lambda event for parameter extraction, Step Functions execution history for pipeline failures).

---

## Architecture Decision: Agent-Native Pipeline (Removing Step Functions)

**Date:** 2026-03-19
**Type:** Deliberate architectural change — not a bug fix

### Context

After the initial infra-agent was working, work began on adding an orchestrator agent. That design conversation surfaced a fundamental question: **in a multi-agent Bedrock system, is Step Functions the right tool for a retry loop driven by model output?**

The answer was no. The decision was made to remove Step Functions entirely and replace the single monolithic action group with four discrete ones — one per pipeline stage.

### What the Original Architecture Looked Like

```
Bedrock Agent
  └── ActionGroupHandler (1 Lambda, 600s timeout)
        └── polls Step Functions every 5s
              └── State Machine
                    ├── GenerateCode   (Lambda)
                    ├── ValidateCode   (Lambda) → CheckValidation → CheckRetryCount → IncrementRetry → RegenerateCode
                    ├── SecurityScan   (Lambda)
                    └── UploadArtifact (Lambda)
```

The agent had one tool: "run the whole pipeline and give me the result." Validation retries were hard-coded into the state machine. The agent had no visibility into intermediate results.

### Why This Was Wrong for an Agent System

**1. Retry logic belonged to the model, not the state machine.**
When Terraform validation fails, the error message carries semantic content: "resource type not found", "missing required argument", "provider version constraint violated". A language model is the right system to read that error and decide how to fix the code. A state machine counter that mechanically retries up to twice provides no such reasoning — it just calls the same Lambda again with the same error appended to a prompt.

**2. The synchronous polling pattern was an anti-pattern that worked around a mismatch.**
The polling Lambda (600s timeout, `time.sleep(5)` in a loop) existed only because the agent expected a synchronous result but Step Functions Standard Workflows are inherently async. The right fix wasn't a polling workaround — it was removing the layer that forced the mismatch.

**3. Two orchestration layers with no clear boundary.**
Bedrock's ReAct loop orchestrated the agent, which orchestrated Step Functions, which orchestrated four Lambdas. The middle layer (Step Functions) added cost and complexity without adding value that the agent's own loop couldn't provide.

### What the New Architecture Looks Like

```
Bedrock Agent
  ├── GenerateIaC  (Lambda) — generate or regenerate with validation_errors context
  ├── ValidateIaC  (Lambda) — terraform init + validate + tflint
  ├── ScanIaC      (Lambda) — Checkov
  └── UploadIaC    (Lambda) — S3
```

The agent instructions describe the workflow. The agent's own ReAct loop decides:
- When to move to the next stage
- Whether to retry generation and how to frame the regeneration prompt
- When to give up on validation and proceed anyway
- What to include in the final response to the user

### What Was Removed

- Step Functions state machine (the `iac-generator` state machine and all its IAM)
- The action group handler Lambda (the 600s polling function)
- `code_regenerator` Lambda (its logic was absorbed into `code_generator` as optional params)
- The concept of retry state being managed by infrastructure

### What Was Gained

- **Better regeneration quality.** The model that generated the code now reads the validation errors as a conversational input and decides how to fix them. This is a richer feedback loop than passing an error string through an `IncrementRetry` Pass state.
- **Simpler infrastructure.** ~100 lines of Terraform removed. No Step Functions IAM, no log delivery policy workarounds, no execution ARN resource patterns.
- **Lower cost.** Step Functions Standard Workflows are billed per state transition. At typical volumes the cost was small, but zero is better.
- **Faster cold paths.** On validation pass the agent moves directly from ValidateIaC to ScanIaC without a state machine round-trip.
- **Visibility into intermediate results.** The agent can surface validation status and security findings to the user alongside the S3 URI, rather than returning a single opaque "pipeline result."

### Trade-offs Accepted

**Less explicit retry bound.** The state machine enforced a hard maximum of 2 retries at the infrastructure level. The new system relies on the agent instructions ("do not retry more than twice"). The model should follow this, but it is a behavioral constraint rather than a structural one.

**Less visual execution graph.** Step Functions provided a clickable execution history in the AWS console. With the agent loop, pipeline visibility comes from Bedrock's orchestration trace and individual Lambda CloudWatch logs.

**Agent session duration limits.** Bedrock agents have a maximum session duration. For very slow generations or many retries, this could theoretically be reached. In practice, three LLM calls plus three Lambda invocations complete well within the limit.

### Lesson Learned

**Match the tool to the decision-maker.** Step Functions is the right tool when a deterministic system — not a model — makes branching decisions. The moment "should I try again?" depends on reading and understanding an error message, that decision belongs to the model. Encoding it as infrastructure is working against the grain of how agents work.

**Architectural fitness for multi-agent systems requires revisiting single-agent assumptions.** The Step Functions design was reasonable for a standalone pipeline. Once it became a sub-agent in an orchestrated system, the overhead became obvious. When adding orchestration, audit whether existing sub-components still make sense at the new level of abstraction.

---

## Architecture Decision: Terraform → Python CDK

**Date:** 2026-03-21
**Type:** Deliberate architectural change — not a bug fix

### Context

The project was initially deployed using Terraform for all AWS resource management. After completing the agent-native pipeline refactor, infrastructure was migrated to Python AWS CDK (Cloud Development Kit).

### Reasons for the Change

**1. CDK is better suited to Bedrock agent resources.**
Bedrock agents, guardrails, action groups, and aliases are supported as first-class CDK L1 constructs (`CfnAgent`, `CfnAgentAlias`, `CfnGuardrail`, etc.). While Terraform community providers exist, they lag the AWS-managed CloudFormation resource types. CDK deploys through CloudFormation, which has native support for all Bedrock resource types as AWS releases them.

**2. Python is already the project language.**
The Lambda handlers, deploy scripts, and test suite are all Python. CDK in Python means all infrastructure is expressed in the same language with the same toolchain — no context switching between HCL and Python, no separate Terraform binary or state management.

**3. No remote state infrastructure required.**
Terraform requires an S3 bucket and DynamoDB table for remote state before any other infrastructure can be created. CDK uses CloudFormation as its state store, which is AWS-managed and requires no bootstrapping beyond `cdk bootstrap` (and even that can be skipped for simple cases by managing the bootstrap stack manually). This removes one entire category of setup complexity for a portfolio project.

**4. CDK drift detection is built in.**
CloudFormation tracks resource state natively. CDK diff shows exactly what will change before deploying. Terraform's plan is equivalent, but requires the remote state infrastructure to exist and be consistent — another moving part.

### What Was Removed

- `agents/infra-agent/terraform/` and `agents/orchestrator/terraform/` directories
- `shared/terraform/` directory (S3 state bucket, DynamoDB lock table, OIDC provider in HCL)
- `shared/terraform/bootstrap/` (the Terraform-in-Terraform bootstrap pattern)
- Terraform remote state S3 bucket and DynamoDB lock table
- `TF_STATE_BUCKET` and `TF_LOCK_TABLE` GitHub Actions secrets

### What Replaced It

- `cdk/` directory with `SharedStack`, `InfraAgentStack`, and `OrchestratorStack`
- All Bedrock, IAM, Lambda, S3, SSM, and CloudWatch resources managed as CDK constructs
- `AWS_DEPLOY_ROLE_ARN` is the only AWS secret required in CI

### Lesson Learned

**IAM drift is invisible to CDK if you use the same policy name for manual updates.** During debugging of a Bedrock permissions issue, the orchestrator role's inline policy was updated manually using `aws iam put-role-policy` with the same CDK-generated policy name. CDK did not detect this as drift on the next deploy and would not have reverted it. Manual IAM changes must use a different policy name than the CDK-generated one, or be explicitly reversed, to prevent silent divergence from the CDK definition.

---

## Issue 4: `associate_agent_collaborator` ValidationException — Missing Bedrock Read Permissions

**Date:** 2026-03-21
**Severity:** High — orchestrator could not be registered with the infra-agent collaborator

### What Happened

After deploying the orchestrator agent stack, `scripts/setup_orchestrator.py` failed at the `associate_agent_collaborator` API call:

```
botocore.errorfactory.ValidationException: You do not have sufficient permissions
to collaborate with this agent alias, or the agent alias does not exist.
```

The infra-agent alias existed and was in PREPARED state. IAM simulation confirmed `bedrock:InvokeAgent` was allowed for the orchestrator role on the correct resource. The error was a `ValidationException`, not an `AccessDeniedException`, which is why IAM simulation showed "allowed."

### Root Cause Analysis

When `associate_agent_collaborator` is called, Bedrock internally invokes read APIs on behalf of the orchestrator's execution role to verify the collaborator alias exists:

- `bedrock:GetAgent`
- `bedrock:GetAgentAlias`
- `bedrock:ListAgents`
- `bedrock:ListAgentAliases`

The orchestrator's CDK-managed IAM policy only included `bedrock:InvokeAgent`. Bedrock could not confirm the alias existed and returned the misleading "no permissions to collaborate" message — a service-level validation error rather than an IAM authorization error.

The misleading error message, combined with IAM simulation returning "allowed" for `InvokeAgent`, sent debugging in the wrong direction for several hours (wrong model type, wrong agent collaboration mode, SCPs, enforced guardrails, and trust policy conditions were all ruled out first).

### Fix

Added a second policy statement to the orchestrator's execution role in `cdk/stacks/orchestrator_stack.py`:

```python
orchestrator_role.add_to_policy(
    iam.PolicyStatement(
        actions=[
            "bedrock:GetAgent",
            "bedrock:GetAgentAlias",
            "bedrock:ListAgents",
            "bedrock:ListAgentAliases",
        ],
        resources=["*"],
    )
)
```

The `associate_agent_collaborator` call succeeded immediately after this was applied.

### Lesson Learned

**A `ValidationException` from a Bedrock control-plane API is not necessarily a data validation error.** Bedrock uses `ValidationException` for internal permission failures when the service cannot confirm a resource exists on behalf of a caller. If `associate_agent_collaborator` returns this error and the alias provably exists, the missing permissions are on the orchestrator's *execution role* (the role Bedrock assumes when acting on behalf of the agent), not on the caller's role.

**IAM simulation only tests the permissions you give it.** Simulating `bedrock:InvokeAgent` confirmed that permission was correct, but did not reveal the missing read permissions that Bedrock needed internally. When a Bedrock API fails with a permissions-adjacent error and simulation passes, simulate the internal read calls Bedrock uses to validate the request, not just the primary action.

---

## Issue 5: GitHub Actions OIDC — CI Role Never Created

**Date:** 2026-03-21
**Severity:** High — all GitHub Actions deploys failing at the credentials step

### What Happened

After adding a GitHub Actions deploy workflow, every run failed immediately:

```
Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```

The GitHub OIDC provider (`token.actions.githubusercontent.com`) had been manually created in IAM. The `AWS_DEPLOY_ROLE_ARN` secret was set in the repository.

### Root Cause Analysis

The IAM role (`multi-agent-system-ci-deploy`) did not exist. `SharedStack` creates both the OIDC provider and the CI deploy role, but only when the `github_repo` CDK context variable is provided. The initial local deployment of `SharedStack` was run without `-c github_repo=...`, so the `if github_repo:` block in `shared_stack.py` was skipped entirely — no role was created.

The OIDC provider was then manually created in the console (correctly), but the CI role was never created by any mechanism. The `AWS_DEPLOY_ROLE_ARN` secret pointed to a role ARN that did not exist.

### Fix

Re-deployed `SharedStack` locally with `github_repo` context and `create_github_oidc_provider=false` (since the provider was already manually created):

```bash
cd cdk && cdk deploy SharedStack --require-approval never \
  -c github_repo=dablotz/infra-agent \
  -c create_github_oidc_provider=false
```

The `CiDeployRoleArn` CloudFormation output was then used to set the `AWS_DEPLOY_ROLE_ARN` GitHub secret.

Additionally, the deploy workflow was updated to always pass `-c create_github_oidc_provider=false` when deploying `SharedStack` — if GitHub Actions is running, the OIDC provider must already exist (it was used to authenticate the job).

### Lesson Learned

**The OIDC provider and CI role are a bootstrapping dependency.** They must exist before GitHub Actions can authenticate. For a CDK project where those resources are managed by CDK itself, a one-time local bootstrap deploy with admin credentials is required before CI can take over. Document this step explicitly — it is easy to create the OIDC provider manually in the console and assume the role will follow.

**Add a CloudFormation output for the CI role ARN.** Without a visible output, the role ARN must be looked up via SSM or the IAM console. A `CiDeployRoleArn` output surfaces it directly in the CDK deploy output, making the copy-paste step obvious.

---

## Architecture Decision: Blue-Green Deployment for Bedrock Agents

**Date:** 2026-03-21
**Type:** Deliberate architectural addition — deployment safety gate

### Context

The initial CI pipeline deployed directly to the production alias on every push to main. A broken deploy would immediately affect production. The goal was to add an integration test gate so that the production alias is only promoted to a new version after end-to-end tests pass.

### Design

```
push to main
  → unit tests
  → CDK deploy (DRAFT updated; staging alias → new numbered version)
  → integration tests against staging alias
  → promote: staging version → production alias
  → orchestrator deploy
```

CDK's `CfnAgentAlias` without `routing_configuration` automatically creates a new numbered version from DRAFT on each deploy. This is used for the staging alias — it gives CI a tested, numbered version to promote without requiring a separate version-creation API call.

The production alias is not managed by CDK. `scripts/promote_agent.py` reads the version from the staging alias (`get_agent_alias`) and applies it to the production alias (`update_agent_alias` or `create_agent_alias`). This keeps the production alias outside of CDK's control so that CDK deploys do not automatically promote it.

### Implementation Issues Encountered

**Issue A: `endSession=True` terminates agent sessions before the response arrives.**

The initial integration test script used `endSession=True` in the `invoke_agent` call, matching the existing smoke test. For a simple prompt with a short response, this works — Bedrock sends the response chunk before the session termination acknowledgment. For complex multi-step agentic workflows (generate → validate → scan → upload), Bedrock sends the session termination message as the only chunk before the pipeline completes. Every integration test returned:

```
Session is terminated as 'endSession' flag is set in request.
```

Fix: remove `endSession=True`. Sessions expire naturally after 30 minutes. The boto3 client was also configured with `read_timeout=300` to handle the 30–90 second response latency of the full pipeline.

**Issue B: `boto3.client('bedrock-agent').create_agent_version()` does not exist.**

`scripts/promote_agent.py` was written with a call to `create_agent_version`, documented in a comment as "available since boto3 1.35.x." This method does not exist in the boto3 Bedrock Agent API — not in 1.35.x and not in 1.42.x. The error on the runner:

```
AttributeError: 'AgentsforBedrock' object has no attribute 'create_agent_version'.
Did you mean: 'delete_agent_version'?
```

Fix: instead of creating a version via boto3, use the CDK-managed staging alias. CloudFormation creates the version automatically when `CfnAgentAlias` is deployed without `routing_configuration`. `promote_agent.py` calls `get_agent_alias` to read the version number from the staging alias's `routingConfiguration`, then calls `update_agent_alias` to apply it to the production alias. Both methods exist in boto3.

### Lesson Learned

**Verify API method existence before writing code that depends on it.** The boto3 Bedrock Agent client does not expose `create_agent_version`, despite the operation existing in the AWS REST API. Check `dir(client)` or the boto3 service reference before assuming an AWS API maps to a boto3 method.

**Use CDK's automatic version creation rather than fighting the API gap.** `CfnAgentAlias` without `routing_configuration` is CloudFormation's built-in mechanism for creating a new agent version on each deploy. Leaning into this eliminated the need for a direct version-creation API call entirely.

**Test streaming agent invocations without `endSession=True` when the pipeline has multiple steps.** The session termination behavior is timing-dependent — it works for fast single-step responses and fails silently for multi-step agentic workflows. The safe default for any agentic workflow is to omit the flag.
