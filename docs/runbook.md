# Multi-Agent IaC System Runbook

## Overview

A Bedrock-powered multi-agent system that generates validated, security-scanned Infrastructure as Code from natural language descriptions and produces runbook documentation for the generated artifacts.

- **Orchestrator agent** (supervisor mode) — receives user requests, delegates to sub-agents, calls GenerateDocs, returns both artifact URIs.
- **Infra-agent** — generates IaC via a 4-action-group ReAct loop: generate → validate → scan → upload.
- **GenerateDocs** — Lambda action group on the orchestrator that reads the uploaded IaC from S3 and produces a Markdown runbook.

---

## Architecture

```
User
 └── Orchestrator Agent (Bedrock, supervisor mode)
       ├── InfraAgent (Bedrock sub-agent, ReAct loop)
       │     ├── GenerateIaC  — Bedrock InvokeModel
       │     ├── ValidateIaC  — terraform + tflint (Lambda layer)
       │     ├── ScanIaC      — checkov (Lambda layer)
       │     └── UploadIaC    — S3 put
       └── GenerateDocs (Lambda action group)
             └── S3 get → Bedrock InvokeModel → S3 put
```

**Key resources:**
| Resource | Name |
|---|---|
| S3 artifact bucket | `infra-agent-iac-output-{account_id}` |
| Lambda layers | `infra-agent-terraform-tools`, `infra-agent-security-tools` |
| SSM — infra-agent ID | `/multi-agent-system/infra-agent/agent-id` |
| SSM — production alias | `/multi-agent-system/infra-agent/alias-id` |
| Bedrock agents | `multi-agent-system-orchestrator`, `infra-agent` |
| CloudWatch (orchestrator) | `/aws/bedrock/agents/multi-agent-system-orchestrator` |

---

## Prerequisites

```bash
# Required tools
aws-cli >= 2.x                        # configured with Bedrock, Lambda, S3, IAM, SSM access
node >= 20 + npm install -g aws-cdk   # CDK CLI
docker + docker compose               # layer builds only (not needed in CI)
python >= 3.12

# Set up Python venv
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Enable Bedrock model access in the console
# Bedrock → Model access → Claude Sonnet 4.5
# (us.anthropic.claude-sonnet-4-5-20250929-v1:0)
```

---

## Deployment

### Quick start (all stacks)

```bash
make all GH_REPO=owner/repo-name
```

Runs: `package-layers → deploy-shared → deploy-infra → promote-infra → deploy-orchestrator`

### Step by step

#### 1. Deploy shared infrastructure

Creates S3 buckets, EventBridge bus, GitHub OIDC provider, and CI deploy role.

```bash
make deploy-shared GH_REPO=owner/repo-name
```

Copy the `CiDeployRoleArn` output — needed for CI setup.

If the OIDC provider already exists in the account:

```bash
cd cdk && cdk deploy SharedStack --require-approval never \
  -c github_repo=owner/repo-name \
  -c create_github_oidc_provider=false
```

#### 2. Build Lambda layers

Layers contain terraform, tflint, and checkov binaries built for Linux `x86_64`.

```bash
make package-layers   # uses Docker; produces shared/lambda_layers/*.zip
```

In CI (GitHub Actions ubuntu runner), layers are built directly without Docker:

```bash
cd agents/infra-agent && make build-layers
```

#### 3. Deploy the infra-agent

Packages Lambda functions (CDK `Code.from_asset`), deploys the stack, creates a staging alias, and runs a smoke test.

```bash
make deploy-infra
```

Output file `/tmp/infra-outputs.json` is written with `AgentId` and `StagingAliasId`.

#### 4. Promote to production

Reads the version from the staging alias and updates (or creates) the production alias. Writes agent-id and alias-id to SSM for the orchestrator deploy.

```bash
make promote-infra
```

#### 5. Deploy the orchestrator

Reads agent-id and alias-id from SSM, deploys the orchestrator stack, then registers the infra-agent as a collaborator and prepares the agent.

```bash
make deploy-orchestrator
```

### Get agent IDs after deployment

```bash
aws ssm get-parameter --name "/multi-agent-system/infra-agent/agent-id" --query "Parameter.Value" --output text
aws ssm get-parameter --name "/multi-agent-system/infra-agent/alias-id" --query "Parameter.Value" --output text
```

Test the agents at **AWS Bedrock console → Agents**.

---

## Running Tests

```bash
# Infra-agent unit tests (no AWS account required)
cd agents/infra-agent && make test

# Orchestrator unit tests
python -m pytest agents/orchestrator/tests/ -v

# Smoke test against a live staging alias
python scripts/smoke_test.py \
  --agent-id <agent_id> \
  --alias-id TSTALIASID \
  --region us-east-1

# Full integration tests (3 end-to-end tests, requires live alias)
python scripts/integration_test.py \
  --agent-id <agent_id> \
  --alias-id <staging_alias_id> \
  --region us-east-1
```

Integration tests assert an `s3://` URI in the response and verify the object exists in S3 with the correct metadata. They time out after 300 seconds per test.

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

The error message contains the ARN that was attempted. Common mismatches:

| ARN type | Correct format |
|---|---|
| Cross-region inference profile | `arn:aws:bedrock:{region}:{account_id}:inference-profile/{model_id}` |
| Foundation model | `arn:aws:bedrock:{region}::foundation-model/{base_model_id}` |

Cross-region inference profiles (`us.anthropic.*`) are account-scoped and include your account ID. Foundation models use `::` (no account ID). CDK derives the base model ID by stripping the `us.` prefix.

Also verify model access is enabled: **Bedrock console → Model access → Claude Sonnet 4.5**.

---

### `associate_agent_collaborator` ValidationException

**Symptom:** `setup_orchestrator.py` fails with a `ValidationException` containing "no permissions to collaborate" — even though `InvokeAgent` appears allowed in IAM policy simulator.

**Cause:** Bedrock calls `GetAgent`, `GetAgentAlias`, `ListAgents`, and `ListAgentAliases` internally on behalf of the orchestrator's execution role to validate the collaborator alias. These are separate from `InvokeAgent` and must also be allowed.

**Fix:** The orchestrator role in CDK already includes these with `resources=["*"]`. If they are missing, add them to `cdk/stacks/orchestrator_stack.py` and redeploy.

---

### Integration Tests Fail / Empty Response

**Symptom:** Integration test times out or returns no S3 URI. Or test logs show: *"Session is terminated as 'endSession' flag is set in request."*

**Cause 1:** `endSession=True` was passed to `invoke_agent`. This causes Bedrock to return a session termination acknowledgement as the only chunk, before the agentic pipeline runs.

**Fix:** Do not pass `endSession=True`. The integration test and smoke test scripts do not include it.

**Cause 2:** Default boto3 `read_timeout` (60s) is shorter than the time a multi-step pipeline takes. The infra-agent pipeline can take 30–90s.

**Fix:** Scripts use `Config(read_timeout=300, connect_timeout=10)` via `botocore.config.Config`. Verify this is present if you write additional scripts.

---

### Lambda Layer Binaries Not Found

**Symptom:** Lambda invocation fails with `RuntimeError: Validation infrastructure failure:` or `Security scan infrastructure failure:`.

**Cause:** The Lambda cannot find `/opt/bin/terraform`, `/opt/bin/tflint`, or `python3 -m checkov.main`.

**Check:**
1. Confirm the layer is attached to the Lambda function in the console.
2. Confirm the layer was built for `python3.12` on Linux `x86_64`. Local builds require Docker (`make package-layers`); CI builds run on an ubuntu runner natively.
3. Confirm binary paths inside the zip match `/opt/bin/terraform` and `/opt/bin/tflint`.

---

### Production Alias Not Updated After Deploy

**Symptom:** Requests still go to an old version after a successful CI run.

**Cause:** The `promote` job only runs if `integration-tests` passes. If integration tests fail, production is unchanged by design.

**Check:** In GitHub Actions, open the workflow run and check the `integration-tests` job. If it failed, the `promote` job will show as skipped.

To promote manually after verifying the staging version is good:

```bash
python scripts/promote_agent.py \
  --agent-id <agent_id> \
  --staging-alias-id <staging_alias_id> \
  --alias-name production \
  --region us-east-1
```

---

### Viewing Logs

All handlers emit structured JSON to CloudWatch. Use Logs Insights to query:

```
fields @timestamp, @message
| filter message = "artifact_uploaded"
| sort @timestamp desc
| limit 20
```

Key log groups:

| Log group | Source |
|---|---|
| `/aws/bedrock/agents/multi-agent-system-orchestrator` | Orchestrator agent |
| `/aws/lambda/infra-agent-code-generator` | IaC generation |
| `/aws/lambda/infra-agent-validator` | Terraform + tflint |
| `/aws/lambda/infra-agent-security-scanner` | Checkov |
| `/aws/lambda/infra-agent-artifact-uploader` | S3 upload |
| `/aws/lambda/multi-agent-system-doc-generator` | Documentation generation |

Key structured log fields: `message`, `iac_type`, `artifact_type`, `regenerating`, `status`, `error_count`, `finding_count`, `s3_uri`, `doc_s3_uri`, `request_id`.

---

## Updating the System

### Updating agent instructions or action group schemas

For the infra-agent, edit files under `agents/infra-agent/bedrock/` then redeploy:

```bash
make deploy-infra
make promote-infra   # after verifying staging
```

For the orchestrator, edit `agents/orchestrator/bedrock/` then:

```bash
make deploy-orchestrator
```

CDK detects content changes and triggers a Bedrock agent re-prepare automatically (infra-agent has `auto_prepare=True`; orchestrator is prepared by `setup_orchestrator.py`).

### Updating Lambda function code

CDK packages Lambda handlers via `Code.from_asset` on each handler directory. A redeploy picks up any code changes:

```bash
make deploy-infra     # for infra-agent Lambdas
make deploy-orchestrator  # for GenerateDocs Lambda
```

### Updating Lambda layers (terraform, tflint, checkov versions)

Edit `TERRAFORM_VERSION` or `TFLINT_VERSION` in `agents/infra-agent/Makefile`, or update the checkov pip install. Then rebuild and redeploy:

```bash
make package-layers
make deploy-infra
make promote-infra
```

### Updating the Bedrock model

Change `BEDROCK_MODEL_ID` in both `cdk/stacks/infra_agent_stack.py` and `cdk/stacks/orchestrator_stack.py`. `BEDROCK_BASE_MODEL_ID` is derived automatically by stripping the cross-region prefix.

---

## Security Notes

- S3 output bucket: SSE-S3 encryption, public access blocked, versioning enabled.
- Bedrock agent roles are scoped to specific inference profile and foundation model ARNs across the three US cross-region routing regions.
- GenerateDocs Lambda IAM is scoped: `s3:GetObject` on `generated/*` prefix only, `s3:PutObject` on `docs/*` prefix only.
- GitHub Actions uses OIDC — no long-lived AWS credentials stored as secrets.
- CI deploy role trust policy is scoped to pushes from the `main` branch only.
- Bedrock guardrails block off-topic requests, prompt injection, profanity, PII (AWS keys, passwords), and harmful content on both the infra-agent and orchestrator.
