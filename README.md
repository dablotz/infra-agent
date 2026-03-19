# Multi-Agent IaC System

A Bedrock-powered multi-agent system for infrastructure as code generation, validation, and documentation. Users interact with an orchestrator agent that coordinates specialized sub-agents — currently the infra-agent is implemented, with the orchestrator and docs-agent planned.

## Planned Architecture

```
User
 └── Orchestrator Agent          (planned)
       ├── Infra-Agent           ✅ implemented
       │     └── Step Functions pipeline
       │           ├── Code Generator   — Bedrock invoke (retries on validation failure)
       │           ├── Validator        — terraform init + validate + tflint
       │           ├── Security Scanner — Checkov
       │           └── Artifact Uploader — S3
       └── Docs-Agent            (planned)
```

### Infra-Agent

Accepts a natural language infrastructure description and produces Terraform, CloudFormation, or CDK that has been syntax-validated and security-scanned before upload to S3. The agent runs the full pipeline synchronously and returns the S3 URI in its response.

## Project Structure

```
├── agents/
│   └── infra-agent/
│       ├── bedrock/           # Agent instructions and action group OpenAPI schema
│       ├── lambda_functions/  # Pipeline Lambda handlers
│       ├── terraform/         # Agent infrastructure (IAM, Step Functions, Bedrock, Guardrails)
│       └── tests/             # Unit tests (65 tests, no AWS account required)
├── shared/
│   ├── terraform/             # Shared S3 buckets, EventBridge bus, Lambda layers bucket
│   ├── lambda_layers/         # Built Lambda layers (terraform-tools, security-tools)
│   └── scripts/               # Layer build scripts
├── docs/
│   ├── runbook.md             # Deployment, operations, and troubleshooting guide
│   └── post-mortem.md         # Root cause analysis of issues encountered during development
├── Dockerfile                 # Build environment (terraform, tflint, checkov, awscli)
├── docker-compose.yml         # Local development container
└── Makefile                   # Top-level deployment targets
```

## Prerequisites

- Docker and Docker Compose (recommended for builds)
- AWS CLI configured with credentials that have permissions for: Bedrock, Lambda, Step Functions, S3, IAM, CloudWatch
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

`make all` runs all three steps in sequence.

### Get the agent ID after deployment

```bash
cd agents/infra-agent/terraform && terraform output agent_id
```

Test the agent at **AWS Bedrock console → Agents → infra-agent**.

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
- Agent infinite loop / empty response
- Lambda layer binary not found
- CloudWatch Logs Insights queries for structured log fields

See [docs/post-mortem.md](docs/post-mortem.md) for root cause analysis of the three main issues encountered during development: Bedrock IAM ARN formats for cross-region inference profiles, synchronous vs asynchronous action group design, and the OpenAPI `requestBody` event structure.

## Roadmap

- **Orchestrator agent** — single entry point that routes requests to sub-agents and coordinates workflows across them via EventBridge
- **Docs-Agent** — receives IaC artifacts via EventBridge and generates human-readable documentation

## Cost Estimate

| Component | Est. monthly cost (light usage) |
|---|---|
| Shared infrastructure (S3, EventBridge) | ~$0.50 |
| Lambda (100 invocations) | ~$0.20 |
| Step Functions (100 executions) | ~$0.03 |
| Bedrock | Variable — depends on token usage |
| **Total** | **~$1–5/month** |
