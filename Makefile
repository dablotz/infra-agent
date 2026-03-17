.PHONY: all deploy-shared deploy-infra deploy-orchestrator deploy-docs destroy-all clean help docker-build docker-run

help:
	@echo "Multi-Agent System Deployment"
	@echo ""
	@echo "Available targets:"
	@echo "  docker-build       - Build Docker container"
	@echo "  docker-run         - Run Docker container"
	@echo "  all                - Deploy everything (shared + all agents)"
	@echo "  deploy-shared      - Deploy shared infrastructure"
	@echo "  deploy-infra       - Deploy infra-agent"
	@echo "  deploy-orchestrator - Deploy orchestrator agent"
	@echo "  deploy-docs        - Deploy docs agent"
	@echo "  destroy-all        - Destroy all infrastructure"
	@echo "  clean              - Clean generated files"

docker-build:
	@echo "Building Docker container..."
	@docker-compose build

docker-run:
	@echo "Starting Docker container..."
	@docker-compose up -d
	@docker-compose exec iac-agent bash

all: deploy-shared deploy-infra

deploy-shared:
	@echo "Deploying shared infrastructure..."
	@cd shared/terraform && terraform init && terraform apply -auto-approve

deploy-infra: deploy-shared
	@echo "Deploying infra-agent..."
	@export LAYERS_BUCKET=$$(cd shared/terraform && terraform output -raw lambda_layers_bucket) && \
		cd agents/infra-agent && $(MAKE) all

deploy-orchestrator:
	@echo "Orchestrator agent not yet implemented"

deploy-docs:
	@echo "Docs agent not yet implemented"

destroy-all:
	@echo "Destroying all infrastructure..."
	@cd agents/infra-agent/terraform && terraform destroy -auto-approve || true
	@cd shared/terraform && terraform destroy -auto-approve

clean:
	@echo "Cleaning generated files..."
	@cd agents/infra-agent && $(MAKE) clean || true
	@rm -rf shared/lambda_layers/terraform_tools shared/lambda_layers/security_tools
	@rm -f shared/lambda_layers/*.zip
	@rm -rf shared/terraform/.terraform shared/terraform/.terraform.lock.hcl
	@rm -rf agents/infra-agent/terraform/.terraform agents/infra-agent/terraform/.terraform.lock.hcl
