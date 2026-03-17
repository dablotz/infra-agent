.PHONY: all clean package-lambdas build-layers deploy prepare-agent create-alias destroy test help

TERRAFORM_VERSION := 1.14.0
TFLINT_VERSION := 0.48.0

help:
	@echo "Infra-Agent Deployment"
	@echo ""
	@echo "Available targets:"
	@echo "  all              - Build and deploy infra-agent"
	@echo "  package-lambdas  - Package Lambda functions"
	@echo "  build-layers     - Build Lambda layers"
	@echo "  deploy           - Deploy infrastructure"
	@echo "  prepare-agent    - Prepare Bedrock agent (requires AGENT_ID)"
	@echo "  create-alias     - Create production alias (requires AGENT_ID)"
	@echo "  destroy          - Destroy infrastructure"
	@echo "  clean            - Remove generated files"

all: package-lambdas build-layers deploy

package-lambdas:
	@echo "Packaging Lambda functions..."
	@for dir in lambda_functions/*/; do \
		func_name=$$(basename "$$dir"); \
		echo "Packaging $$func_name..."; \
		cd "$$dir" && zip -q "../$${func_name}.zip" handler.py && cd ../..; \
	done

build-layers: build-terraform-layer build-security-layer

build-terraform-layer:
	@echo "Building Terraform tools layer..."
	@mkdir -p ../../shared/lambda_layers/terraform_tools/bin
	@cd ../../shared/lambda_layers/terraform_tools/bin && \
		wget -q https://releases.hashicorp.com/terraform/$(TERRAFORM_VERSION)/terraform_$(TERRAFORM_VERSION)_linux_amd64.zip && \
		unzip -q terraform_$(TERRAFORM_VERSION)_linux_amd64.zip && \
		rm terraform_$(TERRAFORM_VERSION)_linux_amd64.zip && \
		wget -q https://github.com/terraform-linters/tflint/releases/download/v$(TFLINT_VERSION)/tflint_linux_amd64.zip && \
		unzip -q tflint_linux_amd64.zip && \
		rm tflint_linux_amd64.zip
	@cd ../../shared/lambda_layers/terraform_tools && zip -qr ../terraform_tools.zip .

build-security-layer:
	@echo "Building security tools layer..."
	@mkdir -p ../../shared/lambda_layers/security_tools/python
	@pip install -q --no-cache-dir --platform manylinux2014_x86_64 --only-binary=:all: checkov -t ../../shared/lambda_layers/security_tools/python/
	@find ../../shared/lambda_layers/security_tools -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find ../../shared/lambda_layers/security_tools -type f -name "*.pyc" -delete
	@cd ../../shared/lambda_layers/security_tools && zip -qr ../security_tools.zip .

deploy:
	@echo "Deploying infra-agent..."
	@cd terraform && \
		terraform init && \
		terraform apply -auto-approve \
			-var="lambda_layers_bucket=$${LAYERS_BUCKET}"

prepare-agent:
	@if [ -z "$(AGENT_ID)" ]; then \
		echo "Error: AGENT_ID required. Usage: make prepare-agent AGENT_ID=<id>"; \
		exit 1; \
	fi
	@aws bedrock-agent prepare-agent --agent-id $(AGENT_ID)

create-alias:
	@if [ -z "$(AGENT_ID)" ]; then \
		echo "Error: AGENT_ID required. Usage: make create-alias AGENT_ID=<id>"; \
		exit 1; \
	fi
	@aws bedrock-agent create-agent-alias --agent-id $(AGENT_ID) --agent-alias-name production

test:
	@echo "Running unit tests..."
	@python -m pytest tests -v


destroy:
	@cd terraform && terraform destroy -auto-approve

clean:
	@rm -rf lambda_functions/*.zip
	@rm -rf terraform/.terraform terraform/.terraform.lock.hcl
