# Bedrock Agent Role
resource "aws_iam_role" "bedrock_agent" {
  name = "${var.project_name}-bedrock-agent-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "bedrock.amazonaws.com"
      }
      Action = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
        }
        ArnLike = {
          "aws:SourceArn" = "arn:aws:bedrock:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:agent/*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "bedrock_agent" {
  name = "bedrock-agent-policy"
  role = aws_iam_role.bedrock_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowModelAndInferenceProfileAccess"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:ListInferenceProfiles",
          "bedrock:GetInferenceProfile"
        ]
        Resource = [
          "arn:aws:bedrock:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}",
          "arn:aws:bedrock:us-east-1::foundation-model/${local.bedrock_base_model_id}",
          "arn:aws:bedrock:us-west-2::foundation-model/${local.bedrock_base_model_id}",
          "arn:aws:bedrock:us-east-2::foundation-model/${local.bedrock_base_model_id}"
        ]
      },
      {
        "Sid" : "AllowMarketplaceSubscription",
        "Effect" : "Allow",
        "Action" : [
          "aws-marketplace:ViewSubscriptions"
        ],
        "Resource" : "*",
        "Condition" : {
          "StringEquals" : {
            "aws:CalledViaLast" : "bedrock.amazonaws.com"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = "bedrock:ApplyGuardrail"
        Resource = aws_bedrock_guardrail.iac_agent.guardrail_arn
      }
    ]
  })
}

# Lambda Code Generator Role
resource "aws_iam_role" "lambda_generator" {
  name = "${var.project_name}-lambda-generator-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_generator" {
  name = "lambda-generator-policy"
  role = aws_iam_role.lambda_generator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "bedrock:InvokeModel"
        Resource = [
          "arn:aws:bedrock:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}",
          "arn:aws:bedrock:us-east-1::foundation-model/${local.bedrock_base_model_id}",
          "arn:aws:bedrock:us-west-2::foundation-model/${local.bedrock_base_model_id}",
          "arn:aws:bedrock:us-east-2::foundation-model/${local.bedrock_base_model_id}"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.lambda_generator.arn}:*"
      }
    ]
  })
}

# Lambda Validator Role
resource "aws_iam_role" "lambda_validator" {
  name = "${var.project_name}-lambda-validator-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_validator" {
  name = "lambda-validator-policy"
  role = aws_iam_role.lambda_validator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.lambda_validator.arn}:*"
      }
    ]
  })
}

# Lambda Security Scanner Role
resource "aws_iam_role" "lambda_scanner" {
  name = "${var.project_name}-lambda-scanner-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_scanner" {
  name = "lambda-scanner-policy"
  role = aws_iam_role.lambda_scanner.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.lambda_scanner.arn}:*"
      }
    ]
  })
}

# Lambda Artifact Uploader Role
resource "aws_iam_role" "lambda_uploader" {
  name = "${var.project_name}-lambda-uploader-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_uploader" {
  name = "lambda-uploader-policy"
  role = aws_iam_role.lambda_uploader.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject"
        ]
        Resource = "${aws_s3_bucket.iac_output.arn}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.lambda_uploader.arn}:*"
      }
    ]
  })
}

# Lambda Action Group Handler Role
resource "aws_iam_role" "lambda_action_group" {
  name = "${var.project_name}-lambda-action-group-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_action_group" {
  name = "lambda-action-group-policy"
  role = aws_iam_role.lambda_action_group.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "states:StartExecution"
        Resource = aws_sfn_state_machine.iac_generator.arn
      },
      {
        Effect   = "Allow"
        Action   = "states:DescribeExecution"
        Resource = "arn:aws:states:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:execution:${var.project_name}-iac-generator:*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.lambda_action_group.arn}:*"
      }
    ]
  })
}

# Step Functions Role
resource "aws_iam_role" "step_functions" {
  name = "${var.project_name}-step-functions-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "states.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "step_functions" {
  name = "step-functions-policy"
  role = aws_iam_role.step_functions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = [
          aws_lambda_function.code_generator.arn,
          aws_lambda_function.validator.arn,
          aws_lambda_function.security_scanner.arn,
          aws_lambda_function.artifact_uploader.arn
        ]
      },
      {
        # Log delivery management actions do not support resource-level restrictions (AWS IAM limitation)
        Effect = "Allow"
        Action = [
          "logs:CreateLogDelivery",
          "logs:GetLogDelivery",
          "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery",
          "logs:ListLogDeliveries",
          "logs:PutResourcePolicy",
          "logs:DescribeResourcePolicies"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "logs:DescribeLogGroups"
        Resource = "${aws_cloudwatch_log_group.step_functions.arn}:*"
      }
    ]
  })
}
