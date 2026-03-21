.PHONY: all build-layers build-terraform-layer build-security-layer \
        clean test help

TERRAFORM_VERSION := 1.14.0
TFLINT_VERSION    := 0.48.0

help:
	@echo "Infra-Agent Build"
	@echo ""
	@echo "Available targets:"
	@echo "  build-layers     - Build Lambda layers (terraform-tools + security-tools)"
	@echo "  test             - Run unit tests"
	@echo "  clean            - Remove generated files"

all: build-layers

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
	@pip install -q --no-cache-dir \
		--platform manylinux2014_x86_64 \
		--only-binary=:all: \
		checkov \
		-t ../../shared/lambda_layers/security_tools/python/
	@find ../../shared/lambda_layers/security_tools -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find ../../shared/lambda_layers/security_tools -type f -name "*.pyc" -delete
	@cd ../../shared/lambda_layers/security_tools && zip -qr ../security_tools.zip .

test:
	@echo "Running unit tests..."
	@python -m pytest tests -v

clean:
	@echo "Nothing to clean (Lambda packaging is handled by CDK)"
