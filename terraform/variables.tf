variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name prefix for all resources"
  type        = string
  default     = "iac-agent"
}

variable "bedrock_model_id" {
  description = "Bedrock model ID for code generation"
  type        = string
  default     = "anthropic.claude-3-5-sonnet-20241022-v2:0"
}
