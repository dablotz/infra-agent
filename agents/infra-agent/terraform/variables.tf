variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

variable "log_retention_days" {
  description = "Days to retain CloudWatch logs"
  type        = number
  default     = 30
}

variable "project_name" {
  description = "Project name prefix for all resources"
  type        = string
  default     = "infra-agent"
}

variable "lambda_layers_bucket" {
  description = "Shared S3 bucket for Lambda layers"
  type        = string
}

variable "bedrock_model_id" {
  description = "Bedrock model ID for code generation"
  type        = string
  default     = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
}
