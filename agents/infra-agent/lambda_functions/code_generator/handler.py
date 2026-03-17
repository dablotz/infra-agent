import json
import boto3
import os
import re


def lambda_handler(event, context, bedrock_client=None):
    bedrock = bedrock_client or boto3.client("bedrock-runtime")

    user_request = event.get("user_request", "")
    iac_type = event.get("iac_type", "terraform")
    validation_errors = event.get("validation_errors", [])
    previous_code = event.get("generated_code", "")
    # retry_count is managed by the Step Functions IncrementRetry state.
    # Passed through unchanged so the pipeline state remains consistent.
    retry_count = event.get("retry_count", 0)

    prompt = _build_prompt(iac_type, user_request, validation_errors, previous_code)

    model_id = os.environ.get("BEDROCK_MODEL_ID", "")

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
    )

    response = bedrock.invoke_model(modelId=model_id, body=body)
    result = json.loads(response["body"].read())

    # Extract generated code based on model type
    if "nova" in model_id.lower():
        generated_code = result["output"]["message"]["content"][0]["text"]
    else:
        generated_code = result["content"][0]["text"]

    # Strip markdown code blocks if present
    generated_code = re.sub(r"^```\w*\n", "", generated_code)
    generated_code = re.sub(r"\n```$", "", generated_code)
    generated_code = generated_code.strip()

    return {
        "statusCode": 200,
        "user_request": user_request,
        "iac_type": iac_type,
        "generated_code": generated_code,
        "retry_count": retry_count,
    }


def _build_prompt(
    iac_type: str,
    user_request: str,
    validation_errors: list,
    previous_code: str = "",
) -> str:
    """Build the LLM prompt for either initial generation or error-guided regeneration.

    When validation_errors is non-empty the prompt includes the previous attempt's
    code and error output so the model can produce a targeted fix rather than
    regenerating from scratch.
    """
    if validation_errors:
        previous_block = ""
        if previous_code:
            previous_block += f"\n\nPREVIOUS ATTEMPT CODE:\n{previous_code}"
        previous_block += (
            "\n\nVALIDATION ERRORS IN PREVIOUS ATTEMPT:\n"
            + "\n".join(validation_errors)
        )
        provider_note = (
            "Include necessary provider configuration with proper version constraints"
        )
        fix_note = "- Fix the validation errors from the previous attempt\n"
    else:
        previous_block = ""
        provider_note = "Include necessary provider configuration and variables"
        fix_note = ""

    return f"""Generate {iac_type} code for the following infrastructure request:

{user_request}{previous_block}

IMPORTANT: Generate the specific resources requested by the user.
Include necessary supporting resources (like IAM policies for access control) but avoid adding unrelated infrastructure.

Requirements:
{fix_note}- Generate complete, working {iac_type} code for the requested resources
- {provider_note}
- Include IAM policies, roles, or other access controls if the user mentions permissions or access
- Do NOT add networking (VPCs, subnets, NAT gateways) unless explicitly requested
- Follow best practices for security
- Output ONLY the raw code without markdown formatting, code blocks, or explanations
- Do not wrap the code in ```hcl or ``` markers
- Start directly with the terraform block

Code:"""
