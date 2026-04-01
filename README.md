# Multi-Agent IaC System

A Bedrock-powered multi-agent system for infrastructure as code generation, validation, and documentation. Users can submit either a natural language description or an architecture diagram (.drawio, .xml, .png, .jpg) and receive validated, security-scanned IaC.

## Architecture

```
                      ┌─ Text request ──────────────────────────────────────────┐
                      │                                                          │
User uploads diagram  │                                                          ▼
(.drawio/.xml/.png/.jpg)                                         Orchestrator Agent (supervisor mode)
        │                                                               ├── Infra-Agent (ReAct loop)
        ▼                                                               │     └── 5 action groups
S3 → upload_router Lambda                                              │           ├── GenerateIaC    — text-only path
  ├── diagram_parser Lambda                                            │           ├── ProcessDiagram — diagram path
  │     (XML → IR + manifest)                                          │           │     (gap resolve + HCL generation)
  └── png_pipeline Lambda                                              │           ├── ValidateIaC    — terraform + tflint
        (Rekognition + Bedrock Vision → IR + manifest)                 │           ├── ScanIaC        — Checkov
              │                                                        │           └── UploadIaC      — S3
              └── Enriched message with [DIAGRAM_CONTEXT] ────────────┘
                                                                       └── GenerateDocs (action group)
                                                                             └── IaC → Bedrock → runbook → S3
```

### Text path

Submit a natural language infrastructure description. The infra-agent's ReAct loop calls GenerateIaC → ValidateIaC → ScanIaC → UploadIaC in sequence. If validation fails it passes errors back to GenerateIaC for a targeted fix and retries up to twice.

### Diagram path

Upload an architecture diagram to S3. The upload_router Lambda detects the file type, invokes the correct parser (XML parser for .drawio/.xml, a two-step Rekognition + Bedrock Vision pipeline for .png/.jpg), and injects the resulting IR and manifest paths into the user message as a `[DIAGRAM_CONTEXT]` block before invoking the orchestrator. The orchestrator then routes to the infra-agent's ProcessDiagram action group, which resolves any missing parameters, generates Terraform HCL, and runs the same validation and security scan as the text path.

Both paths produce the same outputs: a validated HCL artifact and a runbook in S3.

## Project Structure

```
├── .github/workflows/
│   ├── deploy.yml             # Push-to-main: draft deploy → integration tests → promote
│   └── pr.yml                 # PR: unit tests, CDK synth, Checkov scan
├── agents/
│   ├── infra-agent/
│   │   ├── bedrock/           # Agent instructions and 5 OpenAPI action group schemas
│   │   │   └── process_diagram_schema.json  # ProcessDiagram action group
│   │   ├── lambda_functions/
│   │   │   ├── diagram_parser/  # draw.io / Lucidchart XML → IR + manifest
│   │   │   ├── png_pipeline/    # PNG/JPG → Rekognition + Bedrock Vision → IR + manifest
│   │   │   └── iac_agent/       # GenerateIaC, ProcessDiagram, ValidateIaC, ScanIaC, UploadIaC
│   │   │       ├── gap_resolver.py          # Identifies and fills missing parameters
│   │   │       └── terraform_prompt_builder.py  # Builds Bedrock prompt from IR + manifest
│   │   └── tests/             # Unit tests (no AWS account required)
│   └── orchestrator/
│       ├── bedrock/           # Supervisor agent instructions and GenerateDocs schema
│       ├── lambda_functions/
│       │   └── doc_generator/ # Generates runbook; includes manifest_renderer.py
│       └── tests/             # Unit tests for orchestrator Lambdas
├── cdk/
│   ├── app.py                 # CDK app — SharedStack, InfraAgentStack, OrchestratorStack, DiagramPipelineStack
│   ├── stacks/
│   │   ├── shared_stack.py
│   │   ├── infra_agent_stack.py
│   │   ├── orchestrator_stack.py
│   │   └── diagram_pipeline_stack.py  # S3, diagram_parser, png_pipeline, upload_router Lambdas
│   └── requirements-cdk.txt
├── docs/
│   ├── adr/
│   │   └── 001-diagram-pipeline.md  # Decision record: preprocessing step vs. dedicated agent
│   ├── runbook.md             # Deployment, operations, and troubleshooting guide
│   └── post-mortem.md         # Root cause analysis and architecture decisions
├── orchestration/
│   └── upload_router.py       # S3-triggered entry point for diagram uploads
├── schemas/
│   ├── ir_schema.json         # Normalized Intermediate Representation schema
│   └── manifest_schema.json   # Configuration manifest with source tracking
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
- AWS CLI configured with credentials for: Bedrock, Lambda, S3, IAM, SSM, CloudWatch, Rekognition
- Node.js 20+ and `npm install -g aws-cdk` (for CDK CLI)
- Python 3.12+ with a virtual environment (`make venv`)
- Amazon Bedrock model access enabled for two models — enable at **Bedrock console → Model access**:
  - **Claude Sonnet 4.5** (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) — used by infra-agent and orchestrator
  - **Claude Sonnet 4.6** (`us.anthropic.claude-sonnet-4-6-20251001-v1:0`) — used by the PNG vision pipeline

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

`make all` runs: `package-layers → deploy-shared → deploy-infra → promote-infra → deploy-orchestrator → deploy-diagram-pipeline`

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

### Step 6 — Deploy the diagram pipeline

Deploys the DiagramPipelineStack: the diagrams S3 bucket, diagram_parser Lambda, png_pipeline Lambda, and upload_router Lambda. Requires InfraAgentStack and OrchestratorStack to be deployed first (it references the IAC agent role and orchestrator agent IDs).

```bash
make deploy-diagram-pipeline
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

## Using the Diagram Pipeline

Upload an architecture diagram to the diagrams S3 bucket. Include the original user request as object metadata so the upload_router can attach it to the orchestrator invocation.

```bash
# Get the diagrams bucket name
BUCKET=$(aws ssm get-parameter \
  --name "/multi-agent-system/diagram-pipeline/diagrams-bucket" \
  --query "Parameter.Value" --output text)

# Upload a draw.io diagram
aws s3 cp my-architecture.drawio s3://$BUCKET/ \
  --metadata "x-amz-meta-user-request=Generate Terraform for this VPC architecture"

# Upload a PNG diagram
aws s3 cp my-architecture.png s3://$BUCKET/ \
  --metadata "x-amz-meta-user-request=Generate Terraform for this architecture"
```

The S3 event triggers the upload_router Lambda, which:
1. Routes `.drawio`/`.xml` to the XML parser Lambda and `.png`/`.jpg` to the PNG vision pipeline
2. Writes an IR and manifest JSON to `s3://$BUCKET/diagrams/<stem>/`
3. Invokes the orchestrator agent with an enriched message containing the IR and manifest paths

The orchestrator delegates to the infra-agent's ProcessDiagram action group. If the diagram is missing required parameters (e.g., an AMI ID that cannot be inferred), the agent returns a `gaps_found` response listing what it needs. Resubmit with those values filled in via the `user_gaps` field.

The final response from the orchestrator includes:
- An S3 URI for the generated Terraform HCL
- An S3 URI for the configuration manifest (documents every parameter, its source, and any agent reasoning)
- An S3 URI for the generated runbook

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
- **Additional diagram formats** — Visio (.vsdx), Mermaid, and CloudFormation Designer exports each require their own parser. See [docs/adr/001-diagram-pipeline.md](docs/adr/001-diagram-pipeline.md) for the conditions under which adding formats would warrant promoting the pipeline to a dedicated Bedrock agent.

## Cost Estimate

| Component | Est. monthly cost (light usage) |
|---|---|
| Shared infrastructure (S3, EventBridge) | ~$0.50 |
| Lambda (100 invocations across all functions) | ~$0.30 |
| Rekognition (DetectLabels, per PNG/JPG upload) | ~$0.001/image |
| Bedrock (IaC generation, vision analysis, docs) | Variable — depends on token usage |
| **Total** | **~$1–5/month** |

The PNG vision pipeline incurs an additional Bedrock invocation per diagram upload (Claude Sonnet 4.6 via the cross-region inference profile). For light usage this is negligible, but diagram-heavy workflows will see higher Bedrock costs than text-only usage.
