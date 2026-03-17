#!/bin/bash
set -e

echo "Applying Terraform changes with guardrail workaround..."

# Temporarily remove guardrail from state to avoid provider bug
terraform state rm aws_bedrockagent_agent.iac_agent 2>/dev/null || true

# Apply changes
terraform apply "$@"

echo "Terraform apply complete"
