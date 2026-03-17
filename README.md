# Multi-Agent IaC System

A Bedrock-powered multi-agent system for infrastructure as code generation, orchestration, and documentation.

## Architecture

### Agents
- **Infra-Agent**: Generates validated, security-scanned Terraform code
- **Orchestrator** (planned): Coordinates agent workflows
- **Docs-Agent** (planned): Generates documentation for infrastructure code

### Shared Infrastructure
- **EventBridge**: Inter-agent communication bus
- **S3**: Shared artifact storage
- **Lambda Layers**: Shared tools (Terraform, tflint, Checkov)

## Project Structure

```
├── agents/
│   ├── infra-agent/       # IaC generation agent
│   ├── orchestrator/      # Master orchestration agent (planned)
│   └── docs-agent/        # Documentation agent (planned)
├── shared/
│   ├── terraform/         # Shared infrastructure
│   ├── lambda_layers/     # Shared Lambda layers
│   └── scripts/           # Shared scripts
├── Makefile               # Root deployment
└── README.md
```

## Prerequisites

1. Docker and Docker Compose (recommended)
2. AWS CLI configured with appropriate credentials
3. Terraform >= 1.0
4. Access to Amazon Bedrock (Claude models)
5. Python 3.12+

## Quick Start

### Docker (Recommended)

```bash
docker-compose up -d
docker-compose exec iac-agent bash

# Inside container
make all
```

### Local

```bash
make all                              # Deploy shared + infra-agent
cd agents/infra-agent && make prepare-agent AGENT_ID=<id>
cd agents/infra-agent && make create-alias AGENT_ID=<id>
```

## Deployment

### Deploy Everything
```bash
make all
```

### Deploy Individual Components
```bash
make deploy-shared        # Shared infrastructure only
make deploy-infra         # Infra-agent only
```

### Get Agent ID
```bash
cd agents/infra-agent/terraform && terraform output agent_id
```

## Agent Communication Flow

1. User → Orchestrator Agent
2. Orchestrator → Infra-Agent (generate code)
3. Infra-Agent → S3 (upload code) → EventBridge (notify)
4. Orchestrator → Docs-Agent (generate docs)
5. Docs-Agent → S3 (upload docs) → EventBridge (notify)
6. Orchestrator → User (provide locations)

## Cost Estimate

**Shared Infrastructure:**
- EventBridge: ~$0.01/month (minimal events)
- S3: ~$0.50/month (storage + requests)

**Per Agent:**
- Lambda: ~$0.20/month (100 invocations)
- Bedrock: Variable based on model and usage

**Total: ~$1-5/month** for light usage

## Future Enhancements

- Orchestrator agent implementation
- Documentation agent implementation
- CloudFormation/CDK support
- Multi-cloud support
