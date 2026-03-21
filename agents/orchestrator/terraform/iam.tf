# Orchestrator Bedrock Agent Role
resource "aws_iam_role" "bedrock_orchestrator" {
  name = "${var.project_name}-orchestrator-role"

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

resource "aws_iam_role_policy" "bedrock_orchestrator" {
  name = "orchestrator-policy"
  role = aws_iam_role.bedrock_orchestrator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowModelAccess"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:GetInferenceProfile",
          "bedrock:ListInferenceProfiles"
        ]
        Resource = [
          "arn:aws:bedrock:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}",
          "arn:aws:bedrock:us-east-1::foundation-model/${local.bedrock_base_model_id}",
          "arn:aws:bedrock:us-west-2::foundation-model/${local.bedrock_base_model_id}",
          "arn:aws:bedrock:us-east-2::foundation-model/${local.bedrock_base_model_id}"
        ]
      },
      {
        Sid      = "AllowSubAgentInvocation"
        Effect   = "Allow"
        Action   = "bedrock:InvokeAgent"
        Resource = local.infra_agent_alias_arn
      },
      {
        Sid      = "AllowGuardrail"
        Effect   = "Allow"
        Action   = "bedrock:ApplyGuardrail"
        Resource = aws_bedrock_guardrail.orchestrator.guardrail_arn
      }
    ]
  })
}
