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
