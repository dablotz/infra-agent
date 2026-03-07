# IaC Generation Agent

A Bedrock-powered agent that translates natural language into validated, security-scanned Infrastructure as Code.

## Architecture

- **Bedrock Agent**: Orchestrates user interactions and calls action groups
- **Step Functions**: Manages the generation → validation → security scan → storage pipeline
- **Lambda Functions**:
  - Code Generator: Uses Bedrock to generate IaC from natural language
  - Validator: Runs terraform validate and tflint
  - Security Scanner: Runs Checkov for security analysis
  - Artifact Uploader: Stores validated code in S3
- **S3**: Stores generated IaC artifacts with metadata

## Prerequisites

### Docker Setup (Recommended)
1. Docker and Docker Compose
2. AWS CLI configured with appropriate credentials

### Local Setup
1. AWS CLI configured with appropriate credentials
2. Terraform >= 1.0
3. Access to Amazon Bedrock (Claude 3.5 Sonnet model)
4. Python 3.12+ (for Lambda development)
5. pre-commit (optional, for code quality checks)

## Project Structure

```
├── bedrock/              # Bedrock agent configuration
├── lambda_functions/     # Lambda function code
├── lambda_layers/        # Lambda layers (generated)
├── scripts/              # Build and deployment scripts
└── terraform/            # Infrastructure as Code
```

## Setup

### Docker (Recommended)

```bash
# Build and run container
docker-compose up -d
docker-compose exec iac-agent bash

# Inside container
make all
```

### Local Development Environment

```bash
./scripts/setup_dev.sh    # Set up Python venv and install dependencies
source venv/bin/activate  # Activate virtual environment
```

## Deployment

### Quick Start

```bash
make install-hooks                    # Install pre-commit hooks (optional)
make all                              # Build and deploy everything
make prepare-agent AGENT_ID=<id>     # Prepare the agent
make create-alias AGENT_ID=<id>      # Create production alias
```

### Manual Steps

```bash
make install-hooks      # Install pre-commit hooks
make validate           # Run validation checks
make package-lambdas    # Package Lambda functions
make build-layers       # Build Lambda layers
make deploy             # Deploy infrastructure
```

### Get Agent ID

```bash
cd terraform && terraform output agent_id
```

## Usage

Invoke the agent via AWS Console, SDK, or CLI:

```python
import boto3

bedrock_agent = boto3.client('bedrock-agent-runtime')

response = bedrock_agent.invoke_agent(
    agentId='<AGENT_ID>',
    agentAliasId='<ALIAS_ID>',
    sessionId='session-1',
    inputText='Create a VPC with 2 private subnets and a NAT Gateway'
)
```

## Generated Artifacts

All generated IaC files are stored in the S3 bucket with:
- Timestamp-based naming
- Metadata including validation and security scan results
- Versioning enabled

## Security

IAM roles follow least-privilege principles:
- Bedrock access limited to specific model ARN
- Lambda execution scoped to required services
- S3 access restricted to the output bucket

## Future Enhancements

- CloudFormation generation support
- CDK generation support
- Multi-cloud support (Azure, GCP)
- Cost estimation integration
