import json
import logging
import boto3
import os
import re

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _get_props(event):
    return (event.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("properties", []))


def _prop(props, name, default=""):
    return next((p["value"] for p in props if p["name"] == name), default)


def _response(event, status_code, body_dict):
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup", ""),
            "apiPath": event.get("apiPath", ""),
            "httpMethod": event.get("httpMethod", "POST"),
            "httpStatusCode": status_code,
            "responseBody": {
                "application/json": {
                    "body": json.dumps(body_dict)
                }
            }
        }
    }


def lambda_handler(event, context, bedrock_client=None):
    bedrock = bedrock_client or boto3.client("bedrock-runtime")

    props = _get_props(event)
    user_request = _prop(props, "user_request")
    iac_type = _prop(props, "iac_type", "terraform")
    validation_errors = _prop(props, "validation_errors")
    previous_code = _prop(props, "previous_code")

    if not user_request:
        return _response(event, 400, {"error": "user_request is required"})

    model_id = os.environ.get("BEDROCK_MODEL_ID", "")
    prompt = _build_prompt(iac_type, user_request, validation_errors, previous_code)

    logger.info(json.dumps({
        "message": "invoking_model",
        "model_id": model_id,
        "regenerating": bool(validation_errors),
    }))

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    })

    response = bedrock.invoke_model(modelId=model_id, body=body)
    result = json.loads(response["body"].read())

    if "nova" in model_id.lower():
        generated_code = result["output"]["message"]["content"][0]["text"]
    else:
        generated_code = result["content"][0]["text"]

    generated_code = re.sub(r"^```\w*\n", "", generated_code)
    generated_code = re.sub(r"\n```$", "", generated_code)
    generated_code = generated_code.strip()

    logger.info(json.dumps({"message": "code_generated", "iac_type": iac_type}))

    return _response(event, 200, {
        "generated_code": generated_code,
        "iac_type": iac_type,
        "user_request": user_request,
    })


def _build_prompt(
    iac_type: str,
    user_request: str,
    validation_errors: str = "",
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
        previous_block += f"\n\nVALIDATION ERRORS IN PREVIOUS ATTEMPT:\n{validation_errors}"
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
