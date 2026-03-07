# Bedrock Guardrails
resource "aws_bedrock_guardrail" "iac_agent" {
  name                      = "${var.project_name}-guardrail"
  blocked_input_messaging   = "I can only help with Infrastructure as Code generation requests. Please provide a valid infrastructure description."
  blocked_outputs_messaging = "I cannot generate that type of content. Please request valid infrastructure code."
  description               = "Guardrails for IaC generation agent"

  content_policy_config {
    filters_config {
      input_strength  = "HIGH"
      output_strength = "HIGH"
      type            = "HATE"
    }
    filters_config {
      input_strength  = "HIGH"
      output_strength = "HIGH"
      type            = "INSULTS"
    }
    filters_config {
      input_strength  = "HIGH"
      output_strength = "HIGH"
      type            = "SEXUAL"
    }
    filters_config {
      input_strength  = "HIGH"
      output_strength = "HIGH"
      type            = "VIOLENCE"
    }
    filters_config {
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
      type            = "MISCONDUCT"
    }
    filters_config {
      input_strength  = "HIGH"
      output_strength = "NONE"
      type            = "PROMPT_ATTACK"
    }
  }

  topic_policy_config {
    topics_config {
      name = "off-topic"
      examples = [
        "Tell me a joke",
        "What's the weather",
        "Write me a story",
        "Help me with my homework"
      ]
      type       = "DENY"
      definition = "Non-infrastructure topics including personal advice, entertainment, general knowledge questions, or anything unrelated to cloud infrastructure."
    }
  }

  word_policy_config {
    managed_word_lists_config {
      type = "PROFANITY"
    }
    words_config {
      text = "ignore previous instructions"
    }
    words_config {
      text = "disregard all previous"
    }
    words_config {
      text = "forget your instructions"
    }
  }

  sensitive_information_policy_config {
    pii_entities_config {
      action = "BLOCK"
      type   = "AWS_ACCESS_KEY"
    }
    pii_entities_config {
      action = "BLOCK"
      type   = "AWS_SECRET_KEY"
    }
    pii_entities_config {
      action = "BLOCK"
      type   = "PASSWORD"
    }
    pii_entities_config {
      action = "ANONYMIZE"
      type   = "EMAIL"
    }
    pii_entities_config {
      action = "ANONYMIZE"
      type   = "PHONE"
    }
  }
}

resource "aws_bedrock_guardrail_version" "iac_agent" {
  guardrail_arn = aws_bedrock_guardrail.iac_agent.guardrail_arn
  description   = "Production version"
}
