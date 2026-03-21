terraform {
  required_version = ">= 1.14"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.28, < 7.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  # Strip the cross-region prefix to derive the base foundation model ID for ARNs.
  # e.g. "us.anthropic.claude-sonnet-4-5-20250929-v1:0" → "anthropic.claude-sonnet-4-5-20250929-v1:0"
  bedrock_base_model_id = replace(var.bedrock_model_id, "/^[a-z]{2}\\./", "")

  # TSTALIASID is the built-in test alias present on every Bedrock agent, pointing to DRAFT.
  # Replace with a versioned alias ARN once the infra-agent is promoted to production.
  infra_agent_alias_arn = "arn:aws:bedrock:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:agent-alias/${var.infra_agent_id}/TSTALIASID"
}

# Guardrails for the orchestrator
resource "aws_bedrock_guardrail" "orchestrator" {
  name                      = "${var.project_name}-orchestrator-guardrail"
  blocked_input_messaging   = "I can only help with infrastructure management requests."
  blocked_outputs_messaging = "I cannot provide that type of response."
  description               = "Guardrails for the orchestrator agent"

  content_policy_config {
    filters_config {
      input_strength  = "HIGH"
      output_strength = "HIGH"
      type            = "HATE"
    }
    filters_config {
      input_strength  = "HIGH"
      output_strength = "HIGH"
      type            = "INSULTS"
    }
    filters_config {
      input_strength  = "HIGH"
      output_strength = "HIGH"
      type            = "SEXUAL"
    }
    filters_config {
      input_strength  = "HIGH"
      output_strength = "HIGH"
      type            = "VIOLENCE"
    }
    filters_config {
      input_strength  = "MEDIUM"
      output_strength = "NONE"
      type            = "MISCONDUCT"
    }
    filters_config {
      input_strength  = "HIGH"
      output_strength = "NONE"
      type            = "PROMPT_ATTACK"
    }
  }

  sensitive_information_policy_config {
    pii_entities_config {
      action = "BLOCK"
      type   = "AWS_ACCESS_KEY"
    }
    pii_entities_config {
      action = "BLOCK"
      type   = "AWS_SECRET_KEY"
    }
    pii_entities_config {
      action = "BLOCK"
      type   = "PASSWORD"
    }
  }
}

resource "aws_bedrock_guardrail_version" "orchestrator" {
  guardrail_arn = aws_bedrock_guardrail.orchestrator.guardrail_arn
  description   = "Production version"
}

# Orchestrator Bedrock Agent (supervisor mode)
resource "aws_bedrockagent_agent" "orchestrator" {
  agent_name              = "${var.project_name}-orchestrator"
  agent_resource_role_arn = aws_iam_role.bedrock_orchestrator.arn
  foundation_model        = var.bedrock_model_id
  instruction             = file("${path.module}/../bedrock/agent_instructions.txt")
  prepare_agent           = true
  agent_collaboration     = "SUPERVISOR"

  guardrail_configuration {
    guardrail_identifier = aws_bedrock_guardrail.orchestrator.guardrail_id
    guardrail_version    = aws_bedrock_guardrail_version.orchestrator.version
  }
}

# Register the infra-agent as a collaborator
resource "aws_bedrockagent_agent_collaborator" "infra_agent" {
  agent_id      = aws_bedrockagent_agent.orchestrator.id
  agent_version = "DRAFT"

  collaborator_name = "InfraAgent"

  agent_descriptor {
    alias_arn = local.infra_agent_alias_arn
  }

  collaboration_instruction  = "Use this agent for all Infrastructure as Code generation requests. It generates validated, security-scanned Terraform, CloudFormation, or CDK from natural language descriptions and returns the S3 URI of the generated artifact."
  relay_conversation_history = "TO_COLLABORATOR"
}

# CloudWatch log group for the orchestrator agent
resource "aws_cloudwatch_log_group" "orchestrator" {
  name              = "/aws/bedrock/agents/${var.project_name}-orchestrator"
  retention_in_days = var.log_retention_days
}
