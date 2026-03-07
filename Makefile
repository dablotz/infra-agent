.PHONY: all clean package-lambdas build-layers deploy prepare-agent create-alias destroy help install-hooks validate setup docker-build docker-run

TERRAFORM_VERSION := 1.6.0
TFLINT_VERSION := 0.48.0

help:
	@echo "Available targets:"
	@echo "  docker-build     - Build Docker container"
	@echo "  docker-run       - Run Docker container"
	@echo "  setup            - Set up local development environment"
	@echo "  all              - Build everything and deploy"
	@echo "  install-hooks    - Install pre-commit hooks"
	@echo "  validate         - Run pre-commit validation on all files"
	@echo "  package-lambdas  - Package Lambda function code"
	@echo "  build-layers     - Build Lambda layers (terraform tools + security tools)"
	@echo "  deploy           - Run terraform init, plan, and apply"
	@echo "  prepare-agent    - Prepare Bedrock agent (requires AGENT_ID)"
	@echo "  create-alias     - Create production alias (requires AGENT_ID)"
	@echo "  destroy          - Destroy all infrastructure"
	@echo "  clean            - Remove generated files"

docker-build:
	@echo "Building Docker container..."
	@docker-compose build

docker-run:
	@echo "Starting Docker container..."
	@docker-compose up -d
	@docker-compose exec iac-agent bash

setup:
	@chmod +x scripts/setup_dev.sh
	@./scripts/setup_dev.sh

install-hooks:
	@echo "Installing pre-commit hooks..."
	@pip install pre-commit
	@pre-commit install
	@echo "Pre-commit hooks installed"

validate:
	@echo "Running pre-commit validation..."
	@pre-commit run --all-files

all: package-lambdas build-layers deploy

package-lambdas:
	@echo "Packaging Lambda functions..."
	@chmod +x scripts/package_lambdas.sh
	@./scripts/package_lambdas.sh

build-layers: build-terraform-layer build-security-layer

build-terraform-layer:
	@echo "Building Terraform tools layer..."
	@mkdir -p lambda_layers/terraform_tools/bin
	@cd lambda_layers/terraform_tools/bin && \
		wget -q https://releases.hashicorp.com/terraform/$(TERRAFORM_VERSION)/terraform_$(TERRAFORM_VERSION)_linux_amd64.zip && \
		unzip -q terraform_$(TERRAFORM_VERSION)_linux_amd64.zip && \
		rm terraform_$(TERRAFORM_VERSION)_linux_amd64.zip && \
		wget -q https://github.com/terraform-linters/tflint/releases/download/v$(TFLINT_VERSION)/tflint_linux_amd64.zip && \
		unzip -q tflint_linux_amd64.zip && \
		rm tflint_linux_amd64.zip
	@cd lambda_layers/terraform_tools && zip -qr ../terraform_tools.zip .
	@echo "Terraform tools layer built"

build-security-layer:
	@echo "Building security tools layer..."
	@mkdir -p lambda_layers/security_tools/python
	@pip install -q --no-cache-dir --platform manylinux2014_x86_64 --only-binary=:all: checkov -t lambda_layers/security_tools/python/
	@find lambda_layers/security_tools -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find lambda_layers/security_tools -type f -name "*.pyc" -delete
	@cd lambda_layers/security_tools && zip -qr ../security_tools.zip .
	@echo "Security tools layer built"

deploy:
	@echo "Deploying infrastructure..."
	@cd terraform && terraform init
	@cd terraform && terraform plan
	@cd terraform && terraform apply -auto-approve
	@echo "Deployment complete. Run 'make prepare-agent AGENT_ID=<id>' to prepare the agent"

prepare-agent:
	@if [ -z "$(AGENT_ID)" ]; then \
		echo "Error: AGENT_ID is required. Usage: make prepare-agent AGENT_ID=<agent-id>"; \
		exit 1; \
	fi
	@echo "Preparing Bedrock agent..."
	@aws bedrock-agent prepare-agent --agent-id $(AGENT_ID)

create-alias:
	@if [ -z "$(AGENT_ID)" ]; then \
		echo "Error: AGENT_ID is required. Usage: make create-alias AGENT_ID=<agent-id>"; \
		exit 1; \
	fi
	@echo "Creating production alias..."
	@aws bedrock-agent create-agent-alias \
		--agent-id $(AGENT_ID) \
		--agent-alias-name production

destroy:
	@echo "Destroying infrastructure..."
	@if [ -n "$$(cd terraform && terraform output -raw agent_id 2>/dev/null)" ]; then \
		echo "Disabling agent action groups before destroy..."; \
		AGENT_ID=$$(cd terraform && terraform output -raw agent_id); \
		aws bedrock-agent update-agent-action-group \
			--agent-id $$AGENT_ID \
			--agent-version DRAFT \
			--action-group-id $$(aws bedrock-agent list-agent-action-groups --agent-id $$AGENT_ID --agent-version DRAFT --query 'actionGroupSummaries[0].actionGroupId' --output text) \
			--action-group-state DISABLED 2>/dev/null || true; \
		sleep 5; \
	fi
	@cd terraform && terraform destroy -auto-approve

clean:
	@echo "Cleaning generated files..."
	@rm -rf lambda_functions/*.zip
	@rm -rf lambda_layers/terraform_tools lambda_layers/security_tools
	@rm -f lambda_layers/*.zip
	@rm -rf terraform/.terraform terraform/.terraform.lock.hcl
	@echo "Clean complete"
