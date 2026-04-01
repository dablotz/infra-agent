"""
Bedrock Vision Step — uses Claude via Amazon Bedrock to semantically analyze
an AWS architecture diagram image and produce a Normalized IR.

Takes Rekognition output as grounding context and the original S3 image, then
calls Claude to identify all AWS services, directional relationships between
them, and any network boundaries (VPCs, subnets) visible in the diagram.

The model is instructed to return ONLY valid JSON matching ir_schema.json.
"""

import base64
import json
import logging
from typing import Any

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"

# ---------------------------------------------------------------------------
# Terraform resource types the model may assign (keeps the prompt concise)
# ---------------------------------------------------------------------------
_KNOWN_TF_TYPES = (
    "aws_instance, aws_lambda_function, aws_ecs_cluster, aws_ecs_service, "
    "aws_eks_cluster, aws_autoscaling_group, aws_batch_job_definition, "
    "aws_s3_bucket, aws_dynamodb_table, aws_db_instance, aws_rds_cluster, "
    "aws_elasticache_cluster, aws_efs_file_system, "
    "aws_vpc, aws_subnet, aws_security_group, aws_internet_gateway, "
    "aws_nat_gateway, aws_route_table, aws_lb, aws_cloudfront_distribution, "
    "aws_api_gateway_rest_api, aws_apigatewayv2_api, aws_route53_zone, "
    "aws_vpc_endpoint, aws_sns_topic, aws_sqs_queue, aws_kinesis_stream, "
    "aws_kinesis_firehose_delivery_stream, aws_cloudwatch_event_rule, "
    "aws_mq_broker, aws_iam_role, aws_kms_key, aws_secretsmanager_secret, "
    "aws_waf_web_acl, aws_cognito_user_pool, aws_cloudwatch_metric_alarm, "
    "aws_cloudtrail"
)

_SYSTEM_PROMPT = f"""You are an expert AWS solutions architect who specializes in reading
AWS architecture diagrams and extracting their topology as structured data.

Analyze the provided diagram image and extract:
1. Every AWS service or resource shown (nodes / icons)
2. Directional connections between services (edges / arrows)
3. Network boundaries visible in the diagram (VPCs, subnets, security groups)

Return ONLY a single valid JSON object — no markdown fences, no explanation text.
The JSON must conform exactly to this structure:

{{
  "schema_version": "1.0",
  "source_file": "",
  "services": [
    {{
      "id": "<stable sequential id, e.g. svc-1>",
      "type": "<terraform resource type>",
      "label": "<text label from diagram, or service name if unlabeled>",
      "config": {{}}
    }}
  ],
  "relationships": [
    {{
      "source": "<service id>",
      "target": "<service id>",
      "relationship_type": "<one of: connects_to | depends_on | routes_to | contained_by | references>",
      "label": "<edge label text, or null>"
    }}
  ],
  "network": {{
    "vpcs": [
      {{"id": "<id>", "label": "<label>", "cidr_block": "<CIDR or null>"}}
    ],
    "subnets": [
      {{"id": "<id>", "label": "<label>", "vpc_id": "<parent vpc id or null>", "availability_zone": "<az or null>"}}
    ],
    "security_groups": [
      {{"id": "<id>", "label": "<label>", "description": "<desc or null>"}}
    ]
  }}
}}

Rules:
- Assign Terraform resource types from this list: {_KNOWN_TF_TYPES}.
  Use "unknown" only if the AWS service is genuinely not in the list.
- Generate stable sequential IDs: svc-1, svc-2, svc-3, …
- For relationship_type, infer from arrow direction and any edge label present.
  Unlabeled arrows default to "connects_to".
- VPCs and subnets must appear in BOTH services[] and the network{{}} section.
- If a config value (e.g. CIDR block, AZ) appears in a label, extract it into config.
- Leave source_file as an empty string — the caller will set it.
- If the image does not contain an AWS architecture diagram, return an empty
  services array and empty relationships array.
"""


def analyze_diagram(
    s3_bucket: str,
    s3_key: str,
    rekognition_context: list[dict[str, Any]],
    model_id: str = DEFAULT_MODEL_ID,
    bedrock_client=None,
    s3_client=None,
) -> dict[str, Any]:
    """
    Call Claude via Bedrock to analyze a diagram image and return an IR dict.

    Args:
        s3_bucket:          S3 bucket containing the image.
        s3_key:             S3 key of the PNG or JPG image.
        rekognition_context: Structured output from rekognition_step.detect_services().
                            Pass an empty list to rely on Bedrock Vision alone.
        model_id:           Bedrock model ID. Defaults to Claude 3.5 Sonnet v2.
        bedrock_client:     Optional pre-built Bedrock Runtime client (for testing).
        s3_client:          Optional pre-built S3 client (for testing).

    Returns:
        IR dict matching ir_schema.json with source_file set to s3_key.

    Raises:
        ValueError: If the model returns malformed JSON or missing required keys.
    """
    if bedrock_client is None:
        bedrock_client = boto3.client("bedrock-runtime")
    if s3_client is None:
        s3_client = boto3.client("s3")

    # Download and encode the image
    logger.info("Downloading image s3://%s/%s for Bedrock Vision", s3_bucket, s3_key)
    obj = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
    image_bytes = obj["Body"].read()
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    media_type = _media_type_for_key(s3_key)

    user_prompt = _build_user_prompt(rekognition_context)

    logger.info("Calling Bedrock model %s for diagram analysis", model_id)

    request_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "system": _SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": user_prompt,
                    },
                ],
            }
        ],
    }

    response = bedrock_client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(request_body),
    )

    response_body = json.loads(response["body"].read())
    raw_text: str = response_body["content"][0]["text"].strip()
    logger.info("Bedrock returned %d chars", len(raw_text))

    return _parse_and_stamp(raw_text, s3_key)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _media_type_for_key(s3_key: str) -> str:
    lower = s3_key.lower()
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    return "image/png"


def _build_user_prompt(rekognition_context: list[dict[str, Any]]) -> str:
    """Compose the user-facing prompt, optionally including Rekognition grounding."""
    lines = [
        "Analyze this AWS architecture diagram and return the JSON IR as instructed.",
        "",
    ]

    if rekognition_context:
        lines += [
            "## Rekognition Grounding Context",
            "Amazon Rekognition detected the following labels in this image.",
            "Use them as spatial hints — Rekognition does not know AWS icon shapes,",
            "so treat these as approximate guides only:",
            "",
        ]
        for item in rekognition_context:
            bb = item.get("bounding_box")
            if bb:
                location = (
                    f"position ({bb['left']:.2f}, {bb['top']:.2f}), "
                    f"size {bb['width']:.2f}×{bb['height']:.2f}"
                )
            else:
                location = "whole-image"
            hint = item.get("service_hint")
            hint_str = f"  [hint: {hint}]" if hint else ""
            lines.append(
                f"- {item['rekognition_label']} "
                f"(confidence {item['confidence']:.1f}%, {location}){hint_str}"
            )
        lines.append("")

    lines.append(
        "Identify all AWS service icons, their text labels, arrows between them, "
        "and any VPC/subnet/security-group boundaries. "
        "Return ONLY the JSON object — no other text."
    )
    return "\n".join(lines)


def _parse_and_stamp(raw_text: str, source_file: str) -> dict[str, Any]:
    """
    Parse the model's response into an IR dict and stamp source_file.

    Strips markdown code fences if the model included them, validates required
    top-level keys, and ensures the network sub-structure is complete.

    Raises:
        ValueError: On malformed JSON or missing required top-level keys.
    """
    text = raw_text

    # Strip markdown code fences if present (``` or ```json)
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end]).strip()

    try:
        ir = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Bedrock returned malformed JSON: {exc}\nRaw output (first 500 chars): {raw_text[:500]}"
        ) from exc

    required = {"schema_version", "services", "relationships", "network"}
    missing = required - set(ir.keys())
    if missing:
        raise ValueError(f"Bedrock IR response missing required keys: {missing}")

    # Stamp source_file with the actual S3 key (overwrite model's placeholder)
    ir["source_file"] = source_file
    ir.setdefault("schema_version", "1.0")

    # Guarantee all network sub-keys are present
    network = ir.get("network") or {}
    network.setdefault("vpcs", [])
    network.setdefault("subnets", [])
    network.setdefault("security_groups", [])
    ir["network"] = network

    return ir
