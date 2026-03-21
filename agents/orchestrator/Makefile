.PHONY: all deploy validate destroy clean help

help:
	@echo "Orchestrator Agent Deployment"
	@echo ""
	@echo "Available targets:"
	@echo "  all              - Deploy orchestrator"
	@echo "  deploy           - Deploy infrastructure (requires INFRA_AGENT_ID)"
	@echo "  validate         - Validate Terraform configuration"
	@echo "  destroy          - Destroy infrastructure"
	@echo "  clean            - Remove generated files"
	@echo ""
	@echo "Example:"
	@echo "  make deploy INFRA_AGENT_ID=<id>"

all: deploy

deploy:
	@if [ -z "$(INFRA_AGENT_ID)" ]; then \
		echo "Error: INFRA_AGENT_ID is required."; \
		echo "Usage: make deploy INFRA_AGENT_ID=<id>"; \
		echo "Tip:   cd agents/infra-agent/terraform && terraform output agent_id"; \
		exit 1; \
	fi
	@echo "Deploying orchestrator..."
	@cd terraform && \
		terraform init && \
		terraform apply -auto-approve \
			-var="infra_agent_id=$(INFRA_AGENT_ID)"

validate:
	@echo "Validating Terraform configuration..."
	@cd terraform && terraform init -backend=false -input=false && terraform validate

destroy:
	@cd terraform && terraform destroy -auto-approve

clean:
	@rm -rf terraform/.terraform terraform/.terraform.lock.hcl
