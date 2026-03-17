output "agent_id" {
  description = "Bedrock Agent ID"
  value       = aws_bedrockagent_agent.iac_agent.id
}

output "agent_arn" {
  description = "Bedrock Agent ARN"
  value       = aws_bedrockagent_agent.iac_agent.agent_arn
}

output "guardrail_id" {
  description = "Bedrock Guardrail ID"
  value       = aws_bedrock_guardrail.iac_agent.guardrail_id
}

output "guardrail_arn" {
  description = "Bedrock Guardrail ARN"
  value       = aws_bedrock_guardrail.iac_agent.guardrail_arn
}

output "output_bucket" {
  description = "S3 bucket for generated IaC artifacts"
  value       = aws_s3_bucket.iac_output.id
}

output "state_machine_arn" {
  description = "Step Functions state machine ARN"
  value       = aws_sfn_state_machine.iac_generator.arn
}
