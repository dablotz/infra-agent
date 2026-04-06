"""
PNG Pipeline Lambda Handler — processes PNG/JPG architecture diagrams through
a two-step pipeline: Rekognition label detection followed by Bedrock Vision.

Trigger:  S3 event (s3:ObjectCreated:*) on *.png, *.jpg, or *.jpeg objects.
Outputs:  diagrams/{stem}/ir.json        — Normalized IR (ir_schema.json)
          diagrams/{stem}/manifest.json  — Config manifest (manifest_schema.json)

Output format is schema-identical to the XML diagram parser so all downstream
processing is path-agnostic.
"""

import json
import logging
import os
import urllib.parse
from pathlib import PurePosixPath

import boto3

from .rekognition_step import detect_services
from .bedrock_vision_step import analyze_diagram
from utils import slugify

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Minimum number of Rekognition instances with bounding boxes required to use
# Rekognition output as grounding context for Bedrock Vision.
# If fewer instances are found, the pipeline skips grounding and relies solely
# on Bedrock Vision.
_MIN_GROUNDING_INSTANCES = 1



def _build_manifest(services: list[dict], ir_source: str) -> dict:
    """
    Build an initial manifest where every extracted value carries source='parsed'.

    Mirrors the XML parser's _build_manifest exactly so downstream agents see
    an identical structure regardless of whether the source was XML or PNG.
    """
    parameters: list[dict] = []
    for svc in services:
        resource_addr = f"{svc['type']}.{slugify(svc['label'])}"
        parameters.append({
            "parameter": f"{resource_addr}.diagram_id",
            "value": svc["id"],
            "source": "parsed",
            "reasoning": None,
        })
        for k, v in svc.get("config", {}).items():
            parameters.append({
                "parameter": f"{resource_addr}.{k}",
                "value": v,
                "source": "parsed",
                "reasoning": None,
            })
    return {
        "schema_version": "1.0",
        "ir_source": ir_source,
        "parameters": parameters,
    }


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def lambda_handler(
    event,
    context,
    s3_client=None,
    rekognition_client=None,
    bedrock_client=None,
):
    """
    S3-triggered Lambda handler for PNG/JPG architecture diagrams.

    Runs Rekognition label detection, passes results to Bedrock Vision (Claude)
    to produce a Normalized IR and Configuration Manifest, then writes both
    JSON files back to S3.

    Falls back gracefully when Rekognition returns low-confidence results:
    in that case Bedrock Vision runs without grounding context.

    Args:
        event:              AWS S3 event dict (Records[0].s3.bucket / object).
        context:            AWS Lambda context object (unused directly).
        s3_client:          Optional pre-built boto3 S3 client (for testing).
        rekognition_client: Optional pre-built Rekognition client (for testing).
        bedrock_client:     Optional pre-built Bedrock Runtime client (for testing).

    Returns:
        On success: dict with ir_s3_key, manifest_s3_key, service_count.
        On failure: dict with error=True, message, source_file, and null keys.
    """
    if s3_client is None:
        s3_client = boto3.client("s3")

    record = event["Records"][0]
    bucket: str = record["s3"]["bucket"]["name"]
    key: str = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

    logger.info("Processing PNG/JPG diagram: s3://%s/%s", bucket, key)

    # --- Step 1: Rekognition label detection ---
    try:
        rekognition_results = detect_services(
            s3_bucket=bucket,
            s3_key=key,
            rekognition_client=rekognition_client,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Rekognition step failed; proceeding with Bedrock Vision alone.",
            exc_info=True,
        )
        rekognition_results = []

    # Decide whether to use Rekognition output as grounding context
    bounded = [r for r in rekognition_results if r.get("bounding_box")]
    if len(bounded) < _MIN_GROUNDING_INSTANCES:
        logger.info(
            "Rekognition returned %d bounded instances (minimum %d); "
            "relying solely on Bedrock Vision.",
            len(bounded),
            _MIN_GROUNDING_INSTANCES,
        )
        grounding_context: list = []
    else:
        grounding_context = rekognition_results

    # --- Step 2: Bedrock Vision analysis ---
    try:
        ir = analyze_diagram(
            s3_bucket=bucket,
            s3_key=key,
            rekognition_context=grounding_context,
            model_id=os.environ.get("BEDROCK_MODEL_ID", ""),
            bedrock_client=bedrock_client,
            s3_client=s3_client,
        )
    except ValueError as exc:
        logger.error("Bedrock Vision failed to parse diagram: %s", exc)
        return _error_payload(key, str(exc))

    services: list[dict] = ir.get("services", [])
    if not services:
        logger.warning("No AWS services identified in diagram: %s", key)
        return _error_payload(key, "No AWS services could be identified in the diagram.")

    logger.info(
        "Identified %d services, %d relationships",
        len(services),
        len(ir.get("relationships", [])),
    )

    # --- Step 3: Build manifest and write both outputs to S3 ---
    stem = PurePosixPath(key).stem
    ir_key = f"diagrams/{stem}/ir.json"
    manifest_key = f"diagrams/{stem}/manifest.json"

    manifest = _build_manifest(services, ir_source=key)

    s3_client.put_object(
        Bucket=bucket,
        Key=ir_key,
        Body=json.dumps(ir, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    s3_client.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    logger.info("Wrote IR → s3://%s/%s", bucket, ir_key)
    logger.info("Wrote manifest → s3://%s/%s", bucket, manifest_key)

    return {
        "ir_s3_key": ir_key,
        "manifest_s3_key": manifest_key,
        "service_count": len(services),
    }


def _error_payload(source_key: str, message: str) -> dict:
    """Return a structured error payload for unrecognized or unparseable diagrams."""
    return {
        "error": True,
        "source_file": source_key,
        "message": message,
        "ir_s3_key": None,
        "manifest_s3_key": None,
        "service_count": 0,
    }
