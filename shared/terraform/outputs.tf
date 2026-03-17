output "agent_artifacts_bucket" {
  description = "S3 bucket for agent artifacts"
  value       = aws_s3_bucket.agent_artifacts.id
}

output "lambda_layers_bucket" {
  description = "S3 bucket for Lambda layers"
  value       = aws_s3_bucket.lambda_layers.id
}

output "event_bus_name" {
  description = "EventBridge bus for inter-agent communication"
  value       = aws_cloudwatch_event_bus.agent_bus.name
}

output "event_bus_arn" {
  description = "EventBridge bus ARN"
  value       = aws_cloudwatch_event_bus.agent_bus.arn
}
