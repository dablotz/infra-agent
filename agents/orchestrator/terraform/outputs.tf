output "orchestrator_agent_id" {
  description = "Bedrock Orchestrator Agent ID"
  value       = aws_bedrockagent_agent.orchestrator.id
}

output "orchestrator_agent_arn" {
  description = "Bedrock Orchestrator Agent ARN"
  value       = aws_bedrockagent_agent.orchestrator.agent_arn
}

output "guardrail_id" {
  description = "Orchestrator guardrail ID"
  value       = aws_bedrock_guardrail.orchestrator.guardrail_id
}
