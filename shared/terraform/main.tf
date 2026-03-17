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

# Shared S3 bucket for agent artifacts
resource "aws_s3_bucket" "agent_artifacts" {
  bucket        = "${var.project_name}-agent-artifacts-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "agent_artifacts" {
  bucket                  = aws_s3_bucket.agent_artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "agent_artifacts" {
  bucket = aws_s3_bucket.agent_artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "agent_artifacts" {
  bucket = aws_s3_bucket.agent_artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# EventBridge bus for inter-agent communication
resource "aws_cloudwatch_event_bus" "agent_bus" {
  name = "${var.project_name}-agent-bus"
}

# S3 bucket for Lambda layers
resource "aws_s3_bucket" "lambda_layers" {
  bucket        = "${var.project_name}-lambda-layers-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "lambda_layers" {
  bucket = aws_s3_bucket.lambda_layers.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lambda_layers" {
  bucket = aws_s3_bucket.lambda_layers.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "lambda_layers" {
  bucket                  = aws_s3_bucket.lambda_layers.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
