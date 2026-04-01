"""
Upload Router Lambda

Triggered by S3 ObjectCreated events on the diagrams bucket.
Inspects the file extension and routes to the correct parser Lambda,
then forwards the IR and manifest S3 paths to the orchestrating
Bedrock agent as enriched context alongside the original user request.

For text-only requests (no S3 upload), this Lambda is bypassed entirely.

Environment variables
---------------------
DIAGRAM_PARSER_FUNCTION     Name or ARN of the XML / draw.io parser Lambda
PNG_PIPELINE_FUNCTION       Name or ARN of the PNG / JPG vision pipeline Lambda
ORCHESTRATOR_AGENT_ID       Bedrock orchestrator agent ID
ORCHESTRATOR_AGENT_ALIAS_ID Bedrock orchestrator agent alias ID
"""

import json
import logging
import os
import urllib.parse
import uuid

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DIAGRAM_PARSER_FUNCTION = os.environ.get("DIAGRAM_PARSER_FUNCTION", "")
PNG_PIPELINE_FUNCTION = os.environ.get("PNG_PIPELINE_FUNCTION", "")
ORCHESTRATOR_AGENT_ID = os.environ.get("ORCHESTRATOR_AGENT_ID", "")
ORCHESTRATOR_AGENT_ALIAS_ID = os.environ.get("ORCHESTRATOR_AGENT_ALIAS_ID", "")

_XML_EXTENSIONS = frozenset({".drawio", ".xml"})
_PNG_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
_SUPPORTED_EXTENSIONS = _XML_EXTENSIONS | _PNG_EXTENSIONS

# S3 object metadata key for the original user request text.
# Upload clients must set: x-amz-meta-user-request: "<the prompt>"
_USER_REQUEST_META_KEY = "user-request"
_DEFAULT_USER_REQUEST = "Generate infrastructure for the uploaded diagram."


def lambda_handler(event, context, s3_client=None, lambda_client=None, bedrock_agent_client=None):
    """
    Route an S3 upload to the appropriate parser Lambda, then invoke the
    orchestrating Bedrock agent with the resulting IR and manifest paths.

    Expects a standard S3 event notification with at least one Record.
    Returns a dict with statusCode and, on success, ir_s3_key, manifest_s3_key,
    and the Bedrock session_id.
    """
    s3_client = s3_client or boto3.client("s3")
    lambda_client = lambda_client or boto3.client("lambda")
    bedrock_agent_client = bedrock_agent_client or boto3.client("bedrock-agent-runtime")

    record = event["Records"][0]
    bucket = record["s3"]["bucket"]["name"]
    key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

    ext = _file_extension(key)
    if ext not in _SUPPORTED_EXTENSIONS:
        logger.info(json.dumps({"message": "skipped_unsupported_extension", "key": key, "ext": ext}))
        return {"statusCode": 200, "body": f"Skipped: unsupported file type '{ext}'"}

    logger.info(json.dumps({"message": "routing_upload", "bucket": bucket, "key": key, "ext": ext}))
    user_request = _read_user_request(s3_client, bucket, key)

    try:
        parser_result = _invoke_parser(lambda_client, bucket, key, ext)
    except RuntimeError as exc:
        logger.error(json.dumps({"message": "parser_invocation_failed", "error": str(exc)}))
        return {"statusCode": 500, "body": str(exc)}

    if parser_result.get("error"):
        logger.warning(json.dumps({
            "message": "parser_returned_no_services",
            "source": key,
            "detail": parser_result.get("message"),
        }))
        return {
            "statusCode": 422,
            "body": parser_result.get("message", "No AWS services identified in the diagram."),
        }

    ir_s3_key = parser_result["ir_s3_key"]
    manifest_s3_key = parser_result["manifest_s3_key"]
    logger.info(json.dumps({
        "message": "parser_complete",
        "ir_s3_key": ir_s3_key,
        "manifest_s3_key": manifest_s3_key,
        "service_count": parser_result.get("service_count", 0),
    }))

    agent_result = _invoke_orchestrator(
        bedrock_agent_client,
        bucket=bucket,
        user_request=user_request,
        ir_s3_key=ir_s3_key,
        manifest_s3_key=manifest_s3_key,
    )

    logger.info(json.dumps({
        "message": "orchestrator_invoked",
        "session_id": agent_result["session_id"],
    }))

    return {
        "statusCode": 200,
        "ir_s3_key": ir_s3_key,
        "manifest_s3_key": manifest_s3_key,
        "session_id": agent_result["session_id"],
    }


# ── Helpers ────────────────────────────────────────────────────────────────


def _file_extension(key: str) -> str:
    """Return the lowercased file extension including the dot (e.g. '.drawio')."""
    dot_idx = key.rfind(".")
    return key[dot_idx:].lower() if dot_idx != -1 else ""


def _read_user_request(s3_client, bucket: str, key: str) -> str:
    """
    Retrieve the original user request from the S3 object's metadata.
    Falls back to a generic default when the metadata key is absent.
    """
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
        return head.get("Metadata", {}).get(_USER_REQUEST_META_KEY, _DEFAULT_USER_REQUEST)
    except Exception as exc:
        logger.warning(json.dumps({"message": "head_object_failed", "error": str(exc)}))
        return _DEFAULT_USER_REQUEST


def _build_s3_event(bucket: str, key: str) -> dict:
    """Construct a minimal S3 event payload suitable for the parser Lambdas."""
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key},
                }
            }
        ]
    }


def _invoke_parser(lambda_client, bucket: str, key: str, ext: str) -> dict:
    """
    Synchronously invoke the appropriate parser Lambda and return its result dict.
    Raises RuntimeError if the Lambda itself returns a FunctionError.
    """
    function_name = DIAGRAM_PARSER_FUNCTION if ext in _XML_EXTENSIONS else PNG_PIPELINE_FUNCTION
    payload = json.dumps(_build_s3_event(bucket, key)).encode()
    raw = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=payload,
    )
    result = json.loads(raw["Payload"].read())
    if raw.get("FunctionError"):
        raise RuntimeError(f"Parser Lambda error ({function_name}): {result}")
    return result


def _build_enriched_message(
    user_request: str, bucket: str, ir_s3_key: str, manifest_s3_key: str
) -> str:
    """
    Compose the enriched input text for the orchestrating Bedrock agent.

    The [DIAGRAM_CONTEXT] block is parsed by the agent's system prompt to
    extract ir_path and manifest_path without altering the original request.
    """
    return (
        f"{user_request}\n\n"
        f"[DIAGRAM_CONTEXT]\n"
        f"ir_path: s3://{bucket}/{ir_s3_key}\n"
        f"manifest_path: s3://{bucket}/{manifest_s3_key}\n"
        f"[/DIAGRAM_CONTEXT]"
    )


def _invoke_orchestrator(
    bedrock_agent_client,
    *,
    bucket: str,
    user_request: str,
    ir_s3_key: str,
    manifest_s3_key: str,
) -> dict:
    """
    Invoke the orchestrating Bedrock agent with enriched diagram context.
    Returns a dict with 'session_id' and 'completion'.
    """
    session_id = str(uuid.uuid4())
    message = _build_enriched_message(user_request, bucket, ir_s3_key, manifest_s3_key)

    response = bedrock_agent_client.invoke_agent(
        agentId=ORCHESTRATOR_AGENT_ID,
        agentAliasId=ORCHESTRATOR_AGENT_ALIAS_ID,
        sessionId=session_id,
        inputText=message,
    )

    completion_parts: list[str] = []
    for chunk_event in response.get("completion", []):
        chunk = chunk_event.get("chunk", {})
        part = chunk.get("bytes", b"")
        completion_parts.append(
            part.decode("utf-8", errors="replace") if isinstance(part, bytes) else part
        )

    return {"session_id": session_id, "completion": "".join(completion_parts)}
