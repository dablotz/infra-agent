.PHONY: all deploy-shared deploy-infra promote-infra deploy-orchestrator package-layers \
        destroy-all clean help docker-build docker-run venv
# Lambda function packaging is handled by CDK (Code.from_asset on handler dirs).

AWS_REGION ?= us-east-1
CDK         = cd cdk && cdk

help:
	@echo "Multi-Agent System Deployment"
	@echo ""
	@echo "Setup (run once):"
	@echo "  venv                - Create .venv and install Python dependencies"
	@echo "  docker-build        - Build the Docker image used for layer builds"
	@echo ""
	@echo "Build & deploy (run from host — CDK/Node required):"
	@echo "  all                 - Build layers + deploy everything"
	@echo "  package-layers      - Build Lambda layers inside Docker (linux/amd64)"
	@echo "  deploy-shared       - Deploy shared infrastructure (S3, EventBridge, OIDC)"
	@echo "  deploy-infra        - Deploy infra-agent; creates staging alias + smoke test"
	@echo "  promote-infra       - Promote staging version to production alias"
	@echo "  deploy-orchestrator - Deploy orchestrator agent"
	@echo ""
	@echo "Teardown:"
	@echo "  destroy-all         - Destroy all infrastructure"
	@echo "  clean               - Remove generated layer zips and CDK output"
	@echo ""
	@echo "Options:"
	@echo "  GH_REPO=owner/repo  - Enable GitHub OIDC deploy role (deploy-shared)"
	@echo "  AWS_REGION          - AWS region (default: us-east-1)"

venv:
	@python3 -m venv .venv
	@.venv/bin/pip install -q --upgrade pip
	@.venv/bin/pip install -r requirements.txt
	@echo "Virtual environment ready. Activate with: source .venv/bin/activate"

docker-build:
	@echo "Building Docker image..."
	@docker-compose build

docker-run:
	@echo "Starting Docker container (interactive)..."
	@docker-compose run --rm iac-agent bash

# Builds Linux-compatible layer zips by delegating to the Docker container.
# The repo is mounted at /workspace so the zips land back on the host.
package-layers: docker-build
	@echo "Building Lambda layers inside Docker..."
	@docker-compose run --rm iac-agent make -C agents/infra-agent build-layers

all: package-layers deploy-shared deploy-infra promote-infra deploy-orchestrator

deploy-shared:
	@echo "Deploying shared infrastructure..."
	@$(CDK) deploy SharedStack --require-approval never \
		$(if $(GH_REPO),-c github_repo=$(GH_REPO))

# Requires layers to be built first (make package-layers).
# CDK will fail at synth time if the layer zips are missing.
# Creates a staging alias (new numbered version) and runs a smoke test against TSTALIASID.
# Run promote-infra next to shift the production alias to the staged version.
deploy-infra:
	@echo "Deploying infra-agent..."
	@$(CDK) deploy InfraAgentStack --require-approval never \
		--outputs-file /tmp/infra-outputs.json
	@AGENT_ID=$$(python3 -c \
		"import json; d=json.load(open('/tmp/infra-outputs.json')); print(d['InfraAgentStack']['AgentId'])") && \
		python3 scripts/smoke_test.py --agent-id $$AGENT_ID --alias-id TSTALIASID --region $(AWS_REGION)

# Reads the version from the CDK-managed staging alias and updates (or creates)
# the production alias to point to it. Also writes agent-id and alias-id to SSM
# so deploy-orchestrator can pick them up.
promote-infra:
	@echo "Promoting infra-agent staging version to production..."
	@AGENT_ID=$$(python3 -c \
		"import json; d=json.load(open('/tmp/infra-outputs.json')); print(d['InfraAgentStack']['AgentId'])") && \
	STAGING_ALIAS_ID=$$(python3 -c \
		"import json; d=json.load(open('/tmp/infra-outputs.json')); print(d['InfraAgentStack']['StagingAliasId'])") && \
	python3 scripts/promote_agent.py \
		--agent-id $$AGENT_ID \
		--staging-alias-id $$STAGING_ALIAS_ID \
		--region $(AWS_REGION)

deploy-orchestrator:
	@echo "Deploying orchestrator..."
	@INFRA_AGENT_ID=$$(aws ssm get-parameter \
		--name "/multi-agent-system/infra-agent/agent-id" \
		--query "Parameter.Value" --output text) && \
	INFRA_AGENT_ALIAS_ID=$$(aws ssm get-parameter \
		--name "/multi-agent-system/infra-agent/alias-id" \
		--query "Parameter.Value" --output text) && \
	$(CDK) deploy OrchestratorStack --require-approval never \
		--outputs-file /tmp/orchestrator-outputs.json \
		-c infra_agent_id=$$INFRA_AGENT_ID \
		-c infra_agent_alias_id=$$INFRA_AGENT_ALIAS_ID
	@ORCHESTRATOR_ID=$$(python3 -c \
		"import json; d=json.load(open('/tmp/orchestrator-outputs.json')); print(d['OrchestratorStack']['OrchestratorAgentId'])") && \
		python3 scripts/setup_orchestrator.py \
			--orchestrator-id $$ORCHESTRATOR_ID \
			--region $(AWS_REGION)

destroy-all:
	@echo "Destroying all infrastructure..."
	@$(CDK) destroy OrchestratorStack --force || true
	@$(CDK) destroy InfraAgentStack --force || true
	@$(CDK) destroy SharedStack --force

clean:
	@echo "Cleaning generated files..."
	@rm -rf shared/lambda_layers/terraform_tools shared/lambda_layers/security_tools
	@rm -f shared/lambda_layers/*.zip
	@rm -rf cdk/cdk.out
