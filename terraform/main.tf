terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# S3 Bucket for generated IaC artifacts
resource "aws_s3_bucket" "iac_output" {
  bucket = "${var.project_name}-iac-output-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "iac_output" {
  bucket = aws_s3_bucket.iac_output.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "iac_output" {
  bucket = aws_s3_bucket.iac_output.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Lambda Layer for Terraform and tflint binaries
resource "aws_lambda_layer_version" "terraform_tools" {
  filename            = "${path.module}/../lambda_layers/terraform_tools.zip"
  layer_name          = "${var.project_name}-terraform-tools"
  compatible_runtimes = ["python3.12"]
  description         = "Terraform and tflint binaries for validation"
}

# Lambda Layer for security scanning tools
resource "aws_lambda_layer_version" "security_tools" {
  filename            = "${path.module}/../lambda_layers/security_tools.zip"
  layer_name          = "${var.project_name}-security-tools"
  compatible_runtimes = ["python3.12"]
  description         = "Checkov for security scanning"
}

# Lambda: Code Generator
resource "aws_lambda_function" "code_generator" {
  filename      = "${path.module}/../lambda_functions/code_generator.zip"
  function_name = "${var.project_name}-code-generator"
  role          = aws_iam_role.lambda_generator.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300
  memory_size   = 512

  environment {
    variables = {
      BEDROCK_MODEL_ID = var.bedrock_model_id
    }
  }
}

# Lambda: Validator
resource "aws_lambda_function" "validator" {
  filename      = "${path.module}/../lambda_functions/validator.zip"
  function_name = "${var.project_name}-validator"
  role          = aws_iam_role.lambda_validator.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300
  memory_size   = 1024
  layers        = [aws_lambda_layer_version.terraform_tools.arn]
}

# Lambda: Security Scanner
resource "aws_lambda_function" "security_scanner" {
  filename      = "${path.module}/../lambda_functions/security_scanner.zip"
  function_name = "${var.project_name}-security-scanner"
  role          = aws_iam_role.lambda_scanner.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300
  memory_size   = 1024
  layers        = [aws_lambda_layer_version.security_tools.arn]
}

# Lambda: Artifact Uploader
resource "aws_lambda_function" "artifact_uploader" {
  filename      = "${path.module}/../lambda_functions/artifact_uploader.zip"
  function_name = "${var.project_name}-artifact-uploader"
  role          = aws_iam_role.lambda_uploader.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  environment {
    variables = {
      OUTPUT_BUCKET = aws_s3_bucket.iac_output.id
    }
  }
}

# Step Functions State Machine
resource "aws_sfn_state_machine" "iac_generator" {
  name     = "${var.project_name}-iac-generator"
  role_arn = aws_iam_role.step_functions.arn

  definition = jsonencode({
    Comment = "IaC Generation Pipeline"
    StartAt = "GenerateCode"
    States = {
      GenerateCode = {
        Type     = "Task"
        Resource = aws_lambda_function.code_generator.arn
        Next     = "ValidateCode"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "Failed"
        }]
      }
      ValidateCode = {
        Type     = "Task"
        Resource = aws_lambda_function.validator.arn
        Next     = "SecurityScan"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "Failed"
        }]
      }
      SecurityScan = {
        Type     = "Task"
        Resource = aws_lambda_function.security_scanner.arn
        Next     = "UploadArtifact"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "Failed"
        }]
      }
      UploadArtifact = {
        Type     = "Task"
        Resource = aws_lambda_function.artifact_uploader.arn
        End      = true
      }
      Failed = {
        Type  = "Fail"
        Cause = "Pipeline execution failed"
      }
    }
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.step_functions.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }
}

# Bedrock Agent
resource "aws_bedrockagent_agent" "iac_agent" {
  agent_name              = var.project_name
  agent_resource_role_arn = aws_iam_role.bedrock_agent.arn
  foundation_model        = var.bedrock_model_id
  instruction             = file("${path.module}/../bedrock/agent_instructions.txt")
}

resource "aws_bedrockagent_agent_action_group" "iac_generator" {
  agent_id          = aws_bedrockagent_agent.iac_agent.id
  agent_version     = "DRAFT"
  action_group_name = "IaCGeneratorActionGroup"
  action_group_executor {
    lambda = aws_lambda_function.action_group_handler.arn
  }
  api_schema {
    payload = file("${path.module}/../bedrock/action_group_schema.json")
  }
}

# Lambda: Action Group Handler
resource "aws_lambda_function" "action_group_handler" {
  filename      = "${path.module}/../lambda_functions/action_group_handler.zip"
  function_name = "${var.project_name}-action-group-handler"
  role          = aws_iam_role.lambda_action_group.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  environment {
    variables = {
      STATE_MACHINE_ARN = aws_sfn_state_machine.iac_generator.arn
    }
  }
}

resource "aws_lambda_permission" "bedrock_invoke" {
  statement_id  = "AllowBedrockInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.action_group_handler.function_name
  principal     = "bedrock.amazonaws.com"
  source_arn    = aws_bedrockagent_agent.iac_agent.agent_arn
}

# CloudWatch Log Groups
resource "aws_cloudwatch_log_group" "step_functions" {
  name              = "/aws/stepfunctions/${var.project_name}-iac-generator"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "lambda_generator" {
  name              = "/aws/lambda/${var.project_name}-code-generator"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "lambda_validator" {
  name              = "/aws/lambda/${var.project_name}-validator"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "lambda_scanner" {
  name              = "/aws/lambda/${var.project_name}-security-scanner"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "lambda_uploader" {
  name              = "/aws/lambda/${var.project_name}-artifact-uploader"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "lambda_action_group" {
  name              = "/aws/lambda/${var.project_name}-action-group-handler"
  retention_in_days = 7
}
