# Infra-Agent Runbook

## Overview

The infra-agent is an AWS Bedrock agent that generates validated, security-scanned IaC (Terraform, CloudFormation, CDK) from natural language descriptions. It uses a Step Functions pipeline: code generation → validation → security scanning → S3 upload.

---

## Architecture

```
User → Bedrock Agent → Action Group Lambda → Step Functions
                                               ├── Code Generator Lambda (Bedrock)
                                               ├── Validator Lambda (terraform + tflint)
                                               ├── Security Scanner Lambda (checkov)
                                               └── Artifact Uploader Lambda (S3)
```

**Key resources (all prefixed `infra-agent-`):**
- Bedrock agent: `infra-agent`
- State machine: `infra-agent-iac-generator`
- S3 output bucket: `infra-agent-iac-output-{account_id}`
- Lambda layers: `infra-agent-terraform-tools`, `infra-agent-security-tools`

---

## Prerequisites

```bash
# Required tools
terraform >= 1.14
aws-cli >= 2.x
docker (for local builds and layer packaging)
python >= 3.12 (for running tests)

# Required AWS permissions to deploy
bedrock:*, lambda:*, states:*, s3:*, iam:*, cloudwatch:*

# Set up Python venv
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

---

## Deployment

### 1. Deploy shared infrastructure first

The shared stack creates the S3 bucket used for Lambda layers. It must exist before the agent stack.

```bash
cd shared/terraform
terraform init
terraform apply -var="lambda_layers_bucket=<your-bucket-name>"
```

### 2. Package Lambda layers

Lambda layers contain terraform, tflint, and checkov binaries. Build them using the Docker container to ensure Linux-compatible binaries:

```bash
docker compose run --rm infra-agent make package-layers
```

Layers are written to `shared/lambda_layers/`:
- `terraform_tools.zip` — terraform + tflint binaries
- `security_tools.zip` — checkov (Python package)

### 3. Package Lambda functions

```bash
make package-lambdas
```

Zip files are written to `agents/infra-agent/lambda_functions/`.

### 4. Deploy the agent stack

```bash
cd agents/infra-agent/terraform
terraform init
terraform apply -var="lambda_layers_bucket=<your-bucket-name>"
```

### 5. Verify deployment

After `terraform apply` completes, the Bedrock agent is in `PREPARED` state (controlled by `prepare_agent = true` in `main.tf`). Test it in the AWS console under **Amazon Bedrock → Agents → infra-agent**.

---

## Running Tests

```bash
cd agents/infra-agent
source ../../venv/bin/activate
make test
# or directly:
python -m pytest tests/ -v
```

All 65 tests should pass with no warnings.

---

## Troubleshooting

### 403 Access Denied from Bedrock

**Symptom:** `'failureCode': 403, 'failureReason': 'Access denied when calling Bedrock.'` in the agent trace.

**Diagnosis:** Check CloudTrail for the exact resource ARN that was denied.

```
CloudTrail → Event history
  Event source: bedrock.amazonaws.com
  Event name: InvokeModel or Converse
  Error code: AccessDenied
```

The error message will contain the ARN that was attempted. Common mismatches:

| ARN type | Correct format |
|---|---|
| Cross-region inference profile | `arn:aws:bedrock:{region}:{account_id}:inference-profile/{model_id}` |
| Foundation model | `arn:aws:bedrock:{region}::foundation-model/{base_model_id}` |

**Important:** Cross-region inference profiles (`us.anthropic.*`) are account-scoped and need your account ID in the ARN. Foundation models use `::` (no account ID). The `bedrock_base_model_id` local in `main.tf` strips the cross-region prefix (e.g. `us.`) to derive the correct foundation model ID.

Also verify model access is enabled: **Bedrock console → Model access → Claude Sonnet 4.5**.

---

### Agent Returns Empty Response / Infinite Loop

**Symptom:** Agent session completes with an empty final response, or the trace shows the agent repeatedly requesting user input.

**Cause:** The action group handler is synchronous — it polls Step Functions until the pipeline completes (up to 600s). An empty response usually means the pipeline timed out or failed silently, and the agent couldn't formulate a response from the error.

**Diagnosis:** Find the Step Functions execution.

```
Step Functions → State machines → infra-agent-iac-generator
  → Most recent execution → Execution event history
```

Look for the failed state and its `Cause` field. Common failures:
- `GenerateCode` failed → Bedrock access issue (check IAM/403 above)
- `ValidateCode` failed → Lambda layer not attached or binary path wrong
- `SecurityScan` failed → checkov layer not attached

---

### `user_request is required` Error

**Symptom:** Action group returns `{"error": "user_request is required"}` immediately.

**Cause:** The action group handler reads parameters from `requestBody.content["application/json"].properties`. If the Bedrock agent is invoking via a different event structure, the extraction fails.

**Check:** In the Bedrock agent test console, open the trace and find the `actionGroupInvocationInput`. The event sent to Lambda should contain a `requestBody` key (not `parameters`) since the API schema uses `requestBody`.

If the agent was re-deployed without re-preparing, old schema may be cached. Force a prepare: in the Bedrock console, open the agent → **Prepare**.

---

### Lambda Layer Binaries Not Found

**Symptom:** Step Functions execution fails at `ValidateCode` or `SecurityScan` with a `RuntimeError: Validation infrastructure failure: ...` or `Security scan infrastructure failure: ...`.

**Cause:** The Lambda function cannot find `/opt/bin/terraform`, `/opt/bin/tflint`, or `python3 -m checkov.main`.

**Check:**
1. Confirm layer is attached to the Lambda function in the console.
2. Confirm layer was built for `python3.12` runtime on a Linux `x86_64` host (use Docker to build).
3. Confirm binary paths inside the zip match `/opt/bin/terraform` and `/opt/bin/tflint`.

---

### Viewing Logs

All handlers emit structured JSON logs to CloudWatch. Use Logs Insights to query:

```
fields @timestamp, @message
| filter message = "pipeline_failed"
| sort @timestamp desc
| limit 20
```

Key log groups:
- `/aws/lambda/infra-agent-action-group-handler`
- `/aws/lambda/infra-agent-code-generator`
- `/aws/lambda/infra-agent-validator`
- `/aws/lambda/infra-agent-security-scanner`
- `/aws/lambda/infra-agent-artifact-uploader`
- `/aws/stepfunctions/infra-agent-iac-generator`

Key structured log fields: `message`, `iac_type`, `retry_count`, `regenerating`, `status`, `error_count`, `finding_count`, `s3_uri`, `execution_arn`.

---

## Updating the Agent

### Updating agent instructions or the action group schema

Changes to `bedrock/agent_instructions.txt` or `bedrock/action_group_schema.json` require a Terraform apply and a Bedrock agent prepare cycle (handled automatically by `prepare_agent = true`).

### Updating Lambda function code

```bash
make package-lambdas
cd agents/infra-agent/terraform
terraform apply
```

Terraform detects the changed `source_code_hash` and updates only the affected functions.

### Updating the Bedrock model

Change `bedrock_model_id` in `variables.tf` or pass `-var`. The `bedrock_base_model_id` local automatically derives the foundation model ID by stripping the cross-region prefix.

---

## Security Notes

- The S3 output bucket uses SSE-S3 encryption and has all public access blocked.
- The Bedrock agent role is scoped to the specific inference profile ARN and foundation model ARNs across the three US regions the cross-region profile routes through.
- The action group handler validates `iac_type` against `{"terraform", "cloudformation", "cdk"}` and enforces a 4096-character limit on `user_request`.
- Bedrock guardrails are configured to block: off-topic requests, injection attempts, profanity, PII (AWS keys, passwords), and hate/violence/sexual content.
