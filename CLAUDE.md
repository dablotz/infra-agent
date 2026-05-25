# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Setup
```bash
make venv                        # Create .venv and install all Python deps
source .venv/bin/activate
make docker-build                # Build the Docker image used for layer compilation
```

### Tests
```bash
# infra-agent unit tests (run from repo root or agents/infra-agent/)
cd agents/infra-agent && python -m pytest tests -v --tb=short

# Run a single test file
cd agents/infra-agent && python -m pytest tests/unit/test_gap_resolver.py -v

# Run a single test by name
cd agents/infra-agent && python -m pytest tests/unit/test_gap_resolver.py::test_name -v

# Orchestrator tests
cd agents/orchestrator && python -m pytest tests/ -v
```

### Linting
```bash
black --check --line-length 120 agents/         # format check
flake8 --max-line-length=120 agents/            # lint
black agents/                                   # auto-format
```
Pre-commit hooks run black and flake8 scoped to `lambda_functions/**/*.py`.

### Build & Deploy
```bash
make package-layers              # Build Lambda layers inside Docker (required before CDK synth)
make deploy-shared               # S3 buckets, EventBridge, GitHub OIDC
make deploy-infra                # InfraAgent + smoke test against staging alias
make promote-infra               # Promote staging → production alias
make deploy-orchestrator         # OrchestratorAgent + collaborator registration
make all GH_REPO=owner/repo      # Full pipeline

# CDK directly (layers must exist first)
cd cdk && cdk synth
cd cdk && cdk deploy InfraAgentStack --require-approval never
```

---

## Architecture

This is a **Bedrock multi-agent system** that converts natural language or architecture diagrams into validated, security-scanned IaC (Terraform). Two agents operate in a supervisor/executor pattern.

### Agent topology

```
User request (text or diagram upload)
        │
        ▼
OrchestratorAgent  (SUPERVISOR mode, Claude Sonnet 4.5)
  │  delegates to InfraAgent for all IaC work
  └─ GenerateDocs action group  →  doc_generator Lambda
        │
        ▼
InfraAgent  (executor, Claude Sonnet 4.5)
  ├─ ProcessDiagram   →  iac_agent Lambda  (gap resolution + HCL gen from IR)
  ├─ GenerateIaC      →  code_generator Lambda  (text-only path)
  ├─ ValidateIaC      →  validator Lambda  (terraform + tflint)
  ├─ ScanIaC          →  security_scanner Lambda  (Checkov)
  └─ UploadIaC        →  artifact_uploader Lambda  (S3)
```

### Diagram pipeline (separate from agents)

Diagram uploads to S3 trigger the **upload router Lambda**, which preprocesses the diagram into a normalized Intermediate Representation (IR) before invoking the OrchestratorAgent:

- `.drawio` / `.xml` → `diagram_parser` Lambda (defusedxml parsing)
- `.png` / `.jpg` → `png_pipeline` Lambda (Rekognition label detection + Claude Sonnet 4.6 vision)

Both paths produce the same `ir.json` + `manifest.json` schema (see `schemas/`) and write them to S3. The orchestrator receives a `[DIAGRAM_CONTEXT]` block pointing to those S3 keys.

### CDK stacks (deploy order matters)

| Stack | Key resources |
|-------|--------------|
| `SharedStack` | S3 buckets (artifacts, layers), EventBridge bus, GitHub OIDC role, SSM params |
| `InfraAgentStack` | InfraAgent + 5 action group Lambdas, terraform/checkov Lambda layers, Bedrock guardrails |
| `OrchestratorStack` | OrchestratorAgent, doc_generator Lambda; reads infra-agent IDs from SSM |
| `DiagramPipelineStack` | Upload router, diagram_parser, png_pipeline; receives `iac_agent_role` from InfraAgentStack |

`cdk/app.py` wires stacks together. `InfraAgentStack` exports `iac_agent_role` as a Python attribute consumed by `DiagramPipelineStack`. Agent IDs flow cross-stack via SSM (`/multi-agent-system/infra-agent/agent-id` etc.).

### Bedrock agent versioning

CDK always deploys a **DRAFT**. The `TSTALIASID` alias auto-points to DRAFT for integration testing. `scripts/promote_agent.py` shifts the **production alias** to the staged version — this only runs after integration tests pass. Rollback is implicit: production stays on the last-known-good version if tests fail.

### Lambda layers

- `terraform_tools.zip` — Terraform 1.14.0 + tflint 0.48.0 binaries at `/opt/bin/`
- `security_tools.zip` — Checkov Python package at `/opt/python/`

Both must be built via `make package-layers` (Docker, `linux/amd64`) **before** CDK synth. CDK will fail at synth time if the zips are missing.

### Key data structures

- **IR schema** (`schemas/ir_schema.json`): normalized representation of diagram services, relationships, and network topology. Produced by both diagram parsers; consumed by `ProcessDiagram`.
- **Manifest schema** (`schemas/manifest_schema.json`): append-only record of every IaC parameter, its value, source (diagram/user/default), and reasoning. Passed to `GenerateDocs` for runbook generation.

### Testing approach

All unit tests mock AWS clients (boto3, Bedrock, S3). No AWS account needed. `conftest.py` in `agents/infra-agent/tests/` adds each Lambda package directory to `sys.path` so package-local imports resolve identically to Lambda runtime behavior. Integration tests (`scripts/integration_test.py`) hit the real staging alias and require AWS credentials.

### Architecture decisions

See `docs/adr/` for recorded decisions, notably:
- **ADR 001**: Why the diagram pipeline is a Lambda preprocessing step rather than a Bedrock agent
- **ADR 002**: Why `slugify` is duplicated across Lambda packages rather than shared (intentional; separate packaging constraint)
