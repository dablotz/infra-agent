import json
import boto3
import os
import re

bedrock = boto3.client("bedrock-runtime")


def lambda_handler(event, context):
    user_request = event.get("user_request", "")
    iac_type = event.get("iac_type", "terraform")
    validation_errors = event.get("validation_errors", [])
    retry_count = event.get("retry_count", 0)

    error_context = "\n".join(validation_errors) if validation_errors else ""

    prompt = f"""Generate {iac_type} code for the following infrastructure request:

{user_request}

PREVIOUS ATTEMPT HAD VALIDATION ERRORS:
{error_context}

IMPORTANT: Generate the specific resources requested by the user.
Include necessary supporting resources (like IAM policies for access control) but avoid adding unrelated infrastructure.

Requirements:
- Fix the validation errors from the previous attempt
- Generate complete, working {iac_type} code for the requested resources
- Include necessary provider configuration with proper version constraints
- Include IAM policies, roles, or other access controls if the user mentions permissions or access
- Do NOT add networking (VPCs, subnets, NAT gateways) unless explicitly requested
- Follow best practices for security
- Output ONLY the raw code without markdown formatting, code blocks, or explanations
- Do not wrap the code in ```hcl or ``` markers
- Start directly with the terraform block

Code:"""

    model_id = os.environ.get("BEDROCK_MODEL_ID", "")

    if "nova" in model_id.lower():
        body = json.dumps(
            {
                "messages": [{"role": "user", "content": [{"text": prompt}]}],
                "inferenceConfig": {"maxTokens": 4096},
            }
        )
    else:
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            }
        )

    response = bedrock.invoke_model(modelId=model_id, body=body)
    result = json.loads(response["body"].read())

    if "nova" in model_id.lower():
        generated_code = result["output"]["message"]["content"][0]["text"]
    else:
        generated_code = result["content"][0]["text"]

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
