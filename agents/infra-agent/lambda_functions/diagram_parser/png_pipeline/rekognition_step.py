"""
Rekognition Step — detects labels and bounding boxes in architecture diagram PNGs.

Uses Amazon Rekognition DetectLabels to identify regions of interest. The
structured results serve as grounding context for the Bedrock Vision step,
which performs authoritative AWS service identification.

Note: Rekognition does not natively recognize AWS service icons by name.
The service_hint field is a best-effort heuristic; Bedrock Vision is the
source of truth for resource type mapping.
"""

import logging
from typing import Any

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_CONFIDENCE_THRESHOLD = 70.0

# Rekognition label name fragments → best-guess Terraform resource type.
# Keys are lowercase substrings matched against the Rekognition label name.
_LABEL_HINT_MAP: dict[str, str] = {
    "server": "aws_instance",
    "computer": "aws_instance",
    "database": "aws_db_instance",
    "storage": "aws_s3_bucket",
    "bucket": "aws_s3_bucket",
    "lambda": "aws_lambda_function",
    "container": "aws_ecs_cluster",
    "queue": "aws_sqs_queue",
    "gateway": "aws_api_gateway_rest_api",
    "load balancer": "aws_lb",
    "cache": "aws_elasticache_cluster",
    "network": "aws_vpc",
    "security": "aws_security_group",
    "dns": "aws_route53_zone",
    "cdn": "aws_cloudfront_distribution",
    "stream": "aws_kinesis_stream",
    "notification": "aws_sns_topic",
}


def detect_services(
    s3_bucket: str,
    s3_key: str,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    rekognition_client=None,
) -> list[dict[str, Any]]:
    """
    Call Rekognition DetectLabels on an S3 image and return structured results.

    Args:
        s3_bucket:            S3 bucket name containing the image.
        s3_key:               S3 key of the PNG or JPG image.
        confidence_threshold: Minimum Rekognition confidence (0–100) to include
                              a detected label. Defaults to 70.0.
        rekognition_client:   Optional pre-built boto3 Rekognition client;
                              injected by unit tests to avoid real AWS calls.

    Returns:
        List of dicts ordered by descending confidence, each containing:
          - rekognition_label: str  — raw label name from Rekognition
          - confidence:        float — Rekognition confidence score (0–100)
          - bounding_box:      dict | None — normalised {left, top, width, height}
                               in the range 0.0–1.0, or None for whole-image labels
          - service_hint:      str | None — best-guess Terraform resource type
    """
    if rekognition_client is None:
        rekognition_client = boto3.client("rekognition")

    logger.info(
        "Running Rekognition DetectLabels on s3://%s/%s (threshold=%.1f%%)",
        s3_bucket,
        s3_key,
        confidence_threshold,
    )

    response = rekognition_client.detect_labels(
        Image={"S3Object": {"Bucket": s3_bucket, "Name": s3_key}},
        MinConfidence=confidence_threshold,
    )

    results: list[dict[str, Any]] = []

    for label in response.get("Labels", []):
        confidence: float = label.get("Confidence", 0.0)
        if confidence < confidence_threshold:
            continue

        label_name: str = label.get("Name", "")
        service_hint = _guess_service_hint(label_name)

        instances: list[dict] = label.get("Instances", [])
        if instances:
            for instance in instances:
                bb = instance.get("BoundingBox", {})
                results.append({
                    "rekognition_label": label_name,
                    "confidence": confidence,
                    "bounding_box": {
                        "left": bb.get("Left", 0.0),
                        "top": bb.get("Top", 0.0),
                        "width": bb.get("Width", 0.0),
                        "height": bb.get("Height", 0.0),
                    },
                    "service_hint": service_hint,
                })
        else:
            # Whole-image label with no bounding box
            results.append({
                "rekognition_label": label_name,
                "confidence": confidence,
                "bounding_box": None,
                "service_hint": service_hint,
            })

    # Sort descending by confidence so the most certain labels come first
    results.sort(key=lambda r: r["confidence"], reverse=True)

    logger.info(
        "Rekognition returned %d label instances above %.1f%% confidence",
        len(results),
        confidence_threshold,
    )
    return results


def _guess_service_hint(label_name: str) -> str | None:
    """Return a best-guess Terraform resource type for a Rekognition label name."""
    lower = label_name.lower()
    for fragment, tf_type in _LABEL_HINT_MAP.items():
        if fragment in lower:
            return tf_type
    return None
