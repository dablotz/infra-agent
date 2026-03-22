import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_IAC_TYPES = {"terraform", "cloudformation", "cdk"}


def _get_props(event):
    return (
        event.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("properties", [])
    )


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
            },
        },
    }


def _parse_s3_uri(s3_uri):
    """Parse s3://bucket/key into (bucket, key)."""
    match = re.match(r"s3://([^/]+)/(.+)", s3_uri)
    if not match:
        raise ValueError(f"Invalid S3 URI: {s3_uri}")
    return match.group(1), match.group(2)


def lambda_handler(event, context, s3_client=None, bedrock_client=None):
    s3 = s3_client or boto3.client("s3")
    bedrock = bedrock_client or boto3.client("bedrock-runtime")

    props = _get_props(event)
    s3_uri = _prop(props, "s3_uri")
    # artifact_type is a hint from the caller; S3 metadata takes precedence when present.
    artifact_type_hint = _prop(props, "artifact_type", "")

    if not s3_uri:
        return _response(event, 400, {"error": "s3_uri is required"})

    try:
        artifact_bucket, artifact_key = _parse_s3_uri(s3_uri)
    except ValueError as e:
        return _response(event, 400, {"error": str(e)})

    try:
        obj = s3.get_object(Bucket=artifact_bucket, Key=artifact_key)
        artifact_content = obj["Body"].read().decode("utf-8")
        # Prefer the code type stored in S3 metadata by the source agent; fall back
        # to the caller-supplied hint (useful for future agents that use a different
        # metadata key convention).
        s3_metadata = obj.get("Metadata", {})
        artifact_type = (
            s3_metadata.get("iac-type")
            or s3_metadata.get("code-type")
            or artifact_type_hint
            or "unknown"
        )
    except Exception as e:
        logger.error(json.dumps({"message": "s3_read_failed", "s3_uri": s3_uri, "error": str(e)}))
        return _response(event, 400, {"error": f"Failed to read artifact from S3: {e}"})

    logger.info(json.dumps({
        "message": "generating_documentation",
        "s3_uri": s3_uri,
        "artifact_type": artifact_type,
    }))

    model_id = os.environ.get("BEDROCK_MODEL_ID", "")
    prompt = _build_prompt(artifact_content, artifact_type)
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    })

    response = bedrock.invoke_model(modelId=model_id, body=body)
    result = json.loads(response["body"].read())
    documentation = result["content"][0]["text"].strip()

    # Extract request_id from the artifact S3 key (generated/{request_id}/...)
    key_parts = artifact_key.split("/")
    request_id = key_parts[1] if len(key_parts) >= 3 else str(uuid.uuid4())

    output_bucket = os.environ["OUTPUT_BUCKET"]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    doc_key = f"docs/{request_id}/{timestamp}.md"

    s3.put_object(
        Bucket=output_bucket,
        Key=doc_key,
        Body=documentation.encode("utf-8"),
        ContentType="text/markdown",
        Metadata={
            "artifact-s3-uri": s3_uri,
            "artifact-type": artifact_type,
            "request-id": request_id,
        },
    )

    doc_s3_uri = f"s3://{output_bucket}/{doc_key}"
    logger.info(json.dumps({
        "message": "documentation_generated",
        "doc_s3_uri": doc_s3_uri,
        "request_id": request_id,
    }))

    return _response(event, 200, {
        "doc_s3_uri": doc_s3_uri,
        "request_id": request_id,
    })


def _build_prompt(artifact_content: str, artifact_type: str) -> str:
    if artifact_type in _IAC_TYPES:
        return _iac_prompt(artifact_content, artifact_type)
    return _code_prompt(artifact_content, artifact_type)


def _iac_prompt(iac_content: str, iac_type: str) -> str:
    return f"""You are a technical documentation specialist. Analyze the following {iac_type} infrastructure code and generate a clear, practical runbook.

IaC CODE:
{iac_content}

Generate a runbook in Markdown format that includes:

1. **Overview** — What infrastructure this creates and its purpose
2. **Resources Created** — A concise list of all resources with their key configuration details
3. **Prerequisites** — What must exist before deploying (IAM permissions, dependencies, etc.)
4. **Deployment Steps** — Step-by-step instructions to deploy this infrastructure
5. **Verification** — How to confirm the deployment succeeded
6. **Security Considerations** — Notable security settings, access controls, or recommendations
7. **Teardown** — How to safely remove this infrastructure when no longer needed

Be specific and practical. Reference the actual resource names and configurations from the code.
Output only the Markdown runbook — no preamble or explanation."""


def _code_prompt(code_content: str, language: str) -> str:
    return f"""You are a technical documentation specialist. Analyze the following {language} code and generate clear, practical developer documentation.

CODE:
{code_content}

Generate documentation in Markdown format that includes:

1. **Overview** — What this code does and its purpose
2. **Build** — How to compile or build the project, including any required tools or versions
3. **Run** — How to execute the program, with example commands and common flags
4. **Test** — How to run the test suite
5. **Configuration** — Environment variables, config files, or flags that control behavior
6. **Security Considerations** — Notable security practices, input validation, or recommendations
7. **Dependencies** — Key external packages or libraries and their purpose

Be specific and practical. Reference the actual function names, types, and patterns from the code.
Output only the Markdown documentation — no preamble or explanation."""
