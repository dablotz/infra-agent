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
       └── Docs-Agent            (planned)
```

### Infra-Agent

Accepts a natural language infrastructure description and produces Terraform, CloudFormation, or CDK that has been syntax-validated and security-scanned before upload to S3. The agent's own ReAct loop drives the pipeline: it calls each tool in sequence, and if validation fails it passes the errors back to GenerateIaC for a targeted fix before retrying — up to twice.

## Project Structure

```
├── agents/
│   ├── infra-agent/
│   │   ├── bedrock/           # Agent instructions and 4 OpenAPI action group schemas
│   │   ├── lambda_functions/  # Pipeline Lambda handlers (one per action group)
│   │   ├── terraform/         # Agent infrastructure (IAM, Bedrock, Guardrails)
│   │   └── tests/             # Unit tests (57 tests, no AWS account required)
│   └── orchestrator/
│       ├── bedrock/           # Supervisor agent instructions
│       └── terraform/         # Orchestrator agent + collaborator registration
├── shared/
│   ├── terraform/             # Shared S3 buckets, EventBridge bus, Lambda layers bucket
│   ├── lambda_layers/         # Built Lambda layers (terraform-tools, security-tools)
│   └── scripts/               # Layer build scripts
├── docs/
│   ├── runbook.md             # Deployment, operations, and troubleshooting guide
│   └── post-mortem.md         # Root cause analysis and architecture decisions
├── Dockerfile                 # Build environment (terraform, tflint, checkov, awscli)
├── docker-compose.yml         # Local development container
└── Makefile                   # Top-level deployment targets
```

## Prerequisites

- Docker and Docker Compose (recommended for builds)
- AWS CLI configured with credentials that have permissions for: Bedrock, Lambda, S3, IAM, CloudWatch
- Terraform >= 1.14
- Python 3.12+ (for running tests locally)
- Amazon Bedrock model access enabled for **Claude Sonnet 4.5** (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) — enable at **Bedrock console → Model access**

## Quick Start

### Using Docker (recommended)

The container includes all required build tools (terraform, tflint, checkov, awscli).

```bash
# Build and start the container
docker-compose up -d

# Open a shell inside the container
docker-compose exec iac-agent bash

# Inside the container: deploy everything
make all
```

### Local

```bash
make all
```

## Deployment

### Step 0 — Bootstrap remote state (once only)

Creates the S3 bucket and DynamoDB table used as the Terraform remote backend, then writes the connection details to `.terraform-backend`. All subsequent `make` targets read that file automatically — no manual environment setup needed.

```bash
make bootstrap
```

The command prints the `TF_STATE_BUCKET` and `TF_LOCK_TABLE` values at the end. Copy these into GitHub repository secrets for CI (see the CI/CD section below).

### Step 1 — Shared infrastructure

Creates the S3 buckets for Lambda layers and agent artifacts, and an EventBridge bus for future inter-agent communication.

```bash
make deploy-shared
```

### Step 2 — Build Lambda layers

Lambda layers contain the terraform, tflint, and checkov binaries. Build inside Docker to get Linux-compatible binaries.

```bash
docker-compose exec iac-agent make package-layers
```

### Step 3 — Deploy the agent

Packages the Lambda functions, deploys Terraform, and prepares the Bedrock agent.

```bash
make deploy-infra
```

`make all` runs steps 1–3 in sequence.

### Get the agent ID after deployment

```bash
cd agents/infra-agent/terraform && terraform output agent_id
```

Test the agent at **AWS Bedrock console → Agents → infra-agent**.

## CI / CD

GitHub Actions workflows run automatically on pull requests and merges to main.

### PR Checks (no AWS credentials required)

On every pull request:
- **Unit tests** — all Lambda handler tests via pytest
- **Terraform validate** — syntax and schema validation across all modules
- **Checkov scan** — static security analysis of Terraform

### Deploy Pipeline (merge to main)

Merging to main triggers a full deploy and blue/green promotion:

```
merge → unit tests → terraform apply → smoke test → promote → summary
```

The promotion step (`scripts/promote_agent.py`) creates a numbered Bedrock agent version from the newly deployed DRAFT, then shifts the `production` alias to it. The smoke test runs against `TSTALIASID` (DRAFT) before the alias is touched — so production traffic is unaffected by a failed deploy.

**Rollback** is instant: the production alias is not updated until the smoke test passes, so reverting is as simple as reverting the commit.

### One-time setup

1. Run `make bootstrap` and copy the `TF_STATE_BUCKET` and `TF_LOCK_TABLE` outputs.

2. Deploy shared infrastructure with your `github_repo` variable set to create the OIDC role:
   ```bash
   cd shared/terraform && terraform apply -var="github_repo=owner/repo-name"
   ```

3. Copy the `ci_deploy_role_arn` output.

4. Add these **GitHub repository secrets** (`Settings → Secrets → Actions`):

   | Secret | Value |
   |---|---|
   | `AWS_DEPLOY_ROLE_ARN` | `ci_deploy_role_arn` output from step 3 |
   | `TF_STATE_BUCKET` | `state_bucket` output from bootstrap |
   | `TF_LOCK_TABLE` | `lock_table` output from bootstrap |

5. Add this **GitHub repository variable** (`Settings → Variables → Actions`):

   | Variable | Value |
   |---|---|
   | `GITHUB_REPO` | `owner/repo-name` (your GitHub repository) |

## Running Tests

Tests use dependency injection to mock all AWS clients — no AWS account required.

```bash
cd agents/infra-agent
python -m venv venv && source venv/bin/activate
pip install -r ../../requirements.txt
make test
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `bedrock_model_id` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Bedrock cross-region inference profile |
| `project_name` | `infra-agent` | Prefix for all AWS resource names |
| `aws_region` | `us-east-1` | Deployment region |
| `log_retention_days` | `30` | CloudWatch log retention |
| `lambda_layers_bucket` | *(required)* | S3 bucket for Lambda layers (output of shared stack) |

## Security

- Bedrock guardrails block off-topic requests, prompt injection, profanity, PII, and harmful content
- IAM policies are scoped to specific resource ARNs — no `Resource: "*"` on data-plane actions
- S3 output bucket has public access blocked, SSE-S3 encryption, and versioning enabled
- Docker container runs as a non-root user
- Pre-commit hooks enforce credential detection, Terraform formatting, and Python style (`detect-aws-credentials`, `detect-private-key`, `terraform_fmt`, `black`, `flake8`)

## Troubleshooting

See [docs/runbook.md](docs/runbook.md) for:
- Full deployment walkthrough
- Diagnosing 403 Bedrock access denied errors via CloudTrail
- Lambda layer binary not found
- CloudWatch Logs Insights queries for structured log fields

See [docs/post-mortem.md](docs/post-mortem.md) for root cause analysis of issues and architecture decisions encountered during development: Bedrock IAM ARN formats, the OpenAPI `requestBody` event structure, and the rationale for removing Step Functions in favor of the agent's native retry loop.

## Roadmap

- **Docs-Agent** — receives IaC artifacts via EventBridge and generates human-readable documentation
- **Existing IaC RAG lookup** — index an existing codebase into a Bedrock Knowledge Base so the infra-agent can reference established patterns and resource naming conventions when generating new code, producing output that fits the style of existing infrastructure. Deferred due to cost: the required vector store (OpenSearch Serverless or Aurora pgvector) and a Git→S3 sync pipeline are disproportionate for a portfolio deployment. A separate project will explore the data ingestion pipeline independently. See [docs/post-mortem.md](docs/post-mortem.md) for the architectural analysis.

## Cost Estimate

| Component | Est. monthly cost (light usage) |
|---|---|
| Shared infrastructure (S3, EventBridge) | ~$0.50 |
| Lambda (100 invocations) | ~$0.20 |
| Bedrock | Variable — depends on token usage |
| **Total** | **~$1–5/month** |
