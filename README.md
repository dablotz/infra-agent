# Multi-Agent IaC System

A Bedrock-powered multi-agent system for infrastructure as code generation, validation, and documentation. Users interact with an orchestrator that routes requests to specialized sub-agents.

## Architecture

```
User
 └── Orchestrator Agent          ✅ implemented (supervisor mode)
       ├── Infra-Agent           ✅ implemented
       │     └── 4 action groups (agent-native retry loop)
       │           ├── GenerateIaC  — Bedrock invoke (initial + regeneration)
       │           ├── ValidateIaC  — terraform init + validate + tflint
       │           ├── ScanIaC      — Checkov
       │           └── UploadIaC    — S3
       └── GenerateDocs          ✅ implemented (orchestrator action group)
             └── Reads IaC from S3, generates runbook via Bedrock, writes to S3
```

### Infra-Agent

Accepts a natural language infrastructure description and produces Terraform, CloudFormation, or CDK that has been syntax-validated and security-scanned before upload to S3. The agent's own ReAct loop drives the pipeline: it calls each tool in sequence, and if validation fails it passes the errors back to GenerateIaC for a targeted fix before retrying — up to twice.

## Project Structure

```
├── .github/workflows/
│   ├── deploy.yml             # Push-to-main: draft deploy → integration tests → promote
│   └── pr.yml                 # PR: unit tests, CDK synth, Checkov scan
├── agents/
│   ├── infra-agent/
│   │   ├── bedrock/           # Agent instructions and 4 OpenAPI action group schemas
│   │   ├── lambda_functions/  # Pipeline Lambda handlers (one per action group)
│   │   └── tests/             # Unit tests (57 tests, no AWS account required)
│   └── orchestrator/
│       ├── bedrock/           # Supervisor agent instructions and GenerateDocs schema
│       ├── lambda_functions/  # GenerateDocs handler
│       └── tests/             # Unit tests for orchestrator Lambdas
├── cdk/
│   ├── app.py                 # CDK app — SharedStack, InfraAgentStack, OrchestratorStack
│   ├── stacks/                # CDK stack definitions
│   └── requirements-cdk.txt
├── docs/
│   ├── runbook.md             # Deployment, operations, and troubleshooting guide
│   └── post-mortem.md         # Root cause analysis and architecture decisions
├── scripts/
│   ├── smoke_test.py          # Quick agent invocation check (local dev)
│   ├── integration_test.py    # Full pipeline integration tests (CI gate)
│   ├── promote_agent.py       # Promotes staging version to production alias
│   └── setup_orchestrator.py # Registers infra-agent collaborator on orchestrator
├── shared/
│   ├── lambda_layers/         # Built Lambda layers (terraform-tools, security-tools)
│   └── scripts/               # Layer build scripts
├── docker-compose.yml         # Local dev container for building Linux-compatible layers
├── Makefile                   # Top-level deployment targets
└── requirements.txt
```

## Prerequisites

- Docker and Docker Compose (for building Linux-compatible Lambda layers locally)
- AWS CLI configured with credentials for: Bedrock, Lambda, S3, IAM, SSM, CloudWatch
- Node.js 20+ and `npm install -g aws-cdk` (for CDK CLI)
- Python 3.12+ with a virtual environment (`make venv`)
- Amazon Bedrock model access enabled for **Claude Sonnet 4.5** (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) — enable at **Bedrock console → Model access**

## Quick Start

```bash
# Set up Python environment
make venv
source .venv/bin/activate

# Build Linux-compatible Lambda layers (requires Docker)
make package-layers

# Deploy everything (replace with your GitHub repo)
make all GH_REPO=owner/repo-name
```

`make all` runs: `package-layers → deploy-shared → deploy-infra → promote-infra → deploy-orchestrator`

## Deployment

### Step 1 — Shared infrastructure

Creates S3 buckets, an EventBridge bus, a GitHub Actions OIDC provider, and a CI deploy role. Pass `GH_REPO` on the first run to set up GitHub OIDC.

```bash
make deploy-shared GH_REPO=owner/repo-name
```

If the OIDC provider already exists in your account (created manually or by a prior run), skip creating a duplicate:

```bash
cd cdk && cdk deploy SharedStack --require-approval never \
  -c github_repo=owner/repo-name \
  -c create_github_oidc_provider=false
```

Copy the `CiDeployRoleArn` output — you will need it for CI setup.

### Step 2 — Build Lambda layers

Lambda layers contain the terraform, tflint, and checkov binaries. Build inside Docker to get Linux-compatible binaries.

```bash
make package-layers
```

### Step 3 — Deploy the infra-agent

Packages Lambda functions, deploys CDK, creates a staging alias (with a new numbered version), and runs a smoke test.

```bash
make deploy-infra
```

### Step 4 — Promote to production

Reads the version from the staging alias and updates the production alias to point to it.

```bash
make promote-infra
```

### Step 5 — Deploy the orchestrator

Reads the production alias from SSM and deploys the orchestrator agent, then registers the infra-agent as a collaborator.

```bash
make deploy-orchestrator
```

### Get the agent IDs after deployment

```bash
aws ssm get-parameter --name "/multi-agent-system/infra-agent/agent-id" --query "Parameter.Value" --output text
aws ssm get-parameter --name "/multi-agent-system/infra-agent/alias-id" --query "Parameter.Value" --output text
```

Test the agents at **AWS Bedrock console → Agents**.

## CI / CD

GitHub Actions workflows run automatically on pull requests and pushes to main.

### PR Checks (`pr.yml`)

On every pull request:
- **Unit tests** — all Lambda handler tests via pytest (no AWS credentials required)
- **CDK synth** — synthesizes all stacks to catch configuration errors before deploy
- **Checkov scan** — static security analysis of the synthesized CloudFormation templates

### Deploy Pipeline (`deploy.yml`)

Pushing to main triggers a staged deploy with an integration test gate:

```
push to main
  → unit tests
  → deploy draft        (CDK deploys DRAFT + creates staging alias with new version)
  → integration tests   (3 end-to-end tests against staging alias — must all pass)
  → promote             (staging version → production alias, then orchestrator deploy)
  → summary
```

If integration tests fail, the promote job is skipped. The production alias stays on the last good version — no rollback needed.

### One-time CI setup

1. Deploy SharedStack locally with `GH_REPO` set to create the OIDC provider and CI role:
   ```bash
   make deploy-shared GH_REPO=owner/repo-name
   ```

2. Copy the `CiDeployRoleArn` value from the stack output.

3. Add these to your GitHub repository (`Settings → Secrets and variables → Actions`):

   **Secret:**
   | Name | Value |
   |---|---|
   | `AWS_DEPLOY_ROLE_ARN` | `CiDeployRoleArn` output from step 2 |

   **Variable:**
   | Name | Value |
   |---|---|
   | `GH_REPO` | `owner/repo-name` (your GitHub repository) |

## Running Tests

Tests use dependency injection to mock all AWS clients — no AWS account required.

```bash
cd agents/infra-agent
make test
```

## Security

- Bedrock guardrails block off-topic requests, prompt injection, profanity, PII, and harmful content
- IAM policies are scoped to specific resource ARNs — no `Resource: "*"` on data-plane actions
- S3 output bucket has public access blocked, SSE-S3 encryption, and versioning enabled
- GitHub Actions uses OIDC — no long-lived AWS credentials stored as secrets
- CI deploy role trust policy is scoped to pushes from the `main` branch only
- Pre-commit hooks enforce credential detection and Python style (`detect-aws-credentials`, `detect-private-key`, `black`, `flake8`)

## Troubleshooting

See [docs/runbook.md](docs/runbook.md) for:
- Full deployment walkthrough
- Diagnosing 403 Bedrock access denied errors via CloudTrail
- Lambda layer binary not found
- CloudWatch Logs Insights queries for structured log fields

See [docs/post-mortem.md](docs/post-mortem.md) for root cause analysis of issues and architecture decisions encountered during development.

## Roadmap

- **Existing IaC RAG lookup** — index an existing codebase into a Bedrock Knowledge Base so the infra-agent can reference established patterns and resource naming conventions when generating new code, producing output that fits the style of existing infrastructure. Deferred due to cost: the required vector store (OpenSearch Serverless or Aurora pgvector) and a Git→S3 sync pipeline are disproportionate for a portfolio deployment. A separate project will explore the data ingestion pipeline independently. See [docs/post-mortem.md](docs/post-mortem.md) for the architectural analysis.

## Cost Estimate

| Component | Est. monthly cost (light usage) |
|---|---|
| Shared infrastructure (S3, EventBridge) | ~$0.50 |
| Lambda (100 invocations) | ~$0.20 |
| Bedrock | Variable — depends on token usage |
| **Total** | **~$1–5/month** |
