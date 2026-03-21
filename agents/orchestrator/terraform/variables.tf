variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name prefix for all resources"
  type        = string
  default     = "multi-agent-system"
}

variable "bedrock_model_id" {
  description = "Bedrock cross-region inference profile ID"
  type        = string
  default     = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "infra_agent_id" {
  description = "Bedrock Agent ID of the infra-agent (from: cd agents/infra-agent/terraform && terraform output agent_id)"
  type        = string
}

variable "log_retention_days" {
  description = "Days to retain CloudWatch logs"
  type        = number
  default     = 30
}
