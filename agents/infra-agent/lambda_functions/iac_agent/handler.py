"""
IaC generation agent Lambda handler.

Orchestrates the diagram-to-Terraform pipeline:
  1. Load IR and manifest from S3.
  2. Run gap resolution.
     - If unresolvable gaps exist, return them to the orchestrator for user
       resolution instead of proceeding (the orchestrator will call back once
       all gaps are filled).
  3. Build the Bedrock prompt and invoke Claude to generate HCL.
  4. Write the generated HCL and the final enriched manifest to S3.
  5. Return S3 keys for both artifacts plus a signal to proceed to validation.

The manifest is append-only throughout: existing parameter entries are never
overwritten.
"""

import json
import logging
import os
import re
import boto3

from gap_resolver import resolve_gaps, load_from_s3
from terraform_prompt_builder import build_prompt

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Bedrock Action Group helpers  (mirrors the pattern used by other handlers)
# ---------------------------------------------------------------------------


def _get_props(event: dict) -> list[dict]:
    return (
        event.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("properties", [])
    )


def _prop(props: list[dict], name: str, default: str = "") -> str:
    return next((p["value"] for p in props if p["name"] == name), default)


def _response(event: dict, status_code: int, body_dict: dict) -> dict:
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


# ---------------------------------------------------------------------------
# Bedrock invocation helpers
# ---------------------------------------------------------------------------


def _invoke_bedrock(bedrock_client, model_id: str, prompt: str) -> str:
    """Send prompt to Claude via Bedrock and return the raw text response."""
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}],
    })
    response = bedrock_client.invoke_model(modelId=model_id, body=body)
    result = json.loads(response["body"].read())

    if "nova" in model_id.lower():
        text = result["output"]["message"]["content"][0]["text"]
    else:
        text = result["content"][0]["text"]

    # Strip markdown fences in case the model includes them despite instructions
    text = re.sub(r"^```\w*\n", "", text)
    text = re.sub(r"\n```$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# S3 write helpers
# ---------------------------------------------------------------------------


def _put_json(s3_client, bucket: str, key: str, data: dict) -> None:
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def _put_text(s3_client, bucket: str, key: str, text: str) -> None:
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/plain",
    )


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def lambda_handler(
    event,
    context,
    s3_client=None,
    bedrock_client=None,
):
    """Entry point for the IaC generation agent.

    Expected event properties (Bedrock action group format):
      ir_s3_bucket  (str, required): S3 bucket containing the IR and manifest.
      ir_s3_key     (str, required): S3 key of the IR JSON
                                     (e.g. "diagrams/my_arch/ir.json").
      manifest_s3_key (str, required): S3 key of the partial manifest JSON
                                     (e.g. "diagrams/my_arch/manifest.json").
      user_gaps     (str, optional): JSON array of gap objects previously
                                     returned by this handler and now resolved
                                     by the user. Each object must have
                                     "parameter" and "value" keys.

    Returns a Bedrock action group response envelope.  The body will be one of:

      Gaps found (needs user input):
        { "status": "gaps_found", "gaps": [...] }

      Success:
        { "status": "success",
          "hcl_s3_key": "...",
          "manifest_s3_key": "...",
          "s3_bucket": "..." }
    """
    s3 = s3_client or boto3.client("s3")
    bedrock = bedrock_client or boto3.client("bedrock-runtime")

    props = _get_props(event)
    ir_bucket = _prop(props, "ir_s3_bucket")
    ir_key = _prop(props, "ir_s3_key")
    manifest_key = _prop(props, "manifest_s3_key")
    user_gaps_raw = _prop(props, "user_gaps")

    if not ir_bucket or not ir_key or not manifest_key:
        return _response(event, 400, {
            "error": "ir_s3_bucket, ir_s3_key, and manifest_s3_key are all required"
        })

    # ---- Load IR and manifest from S3 ----
    try:
        ir = load_from_s3(ir_bucket, ir_key, s3)
        manifest = load_from_s3(ir_bucket, manifest_key, s3)
    except Exception as exc:
        logger.error(json.dumps({"message": "s3_load_failed", "error": str(exc)}))
        return _response(event, 500, {"error": f"Failed to load artifacts from S3: {exc}"})

    # ---- Merge any user-resolved gaps into the manifest (append-only) ----
    if user_gaps_raw:
        try:
            user_resolved: list[dict] = json.loads(user_gaps_raw)
        except json.JSONDecodeError:
            return _response(event, 400, {"error": "user_gaps must be a valid JSON array"})

        existing_keys = {p["parameter"] for p in manifest["parameters"]}
        for gap in user_resolved:
            if gap.get("parameter") and gap["parameter"] not in existing_keys:
                manifest["parameters"].append({
                    "parameter": gap["parameter"],
                    "value": str(gap["value"]),
                    "source": "user_provided",
                    "reasoning": None,
                })
        logger.info(json.dumps({
            "message": "user_gaps_merged",
            "count": len(user_resolved),
        }))

    # ---- Gap resolution ----
    try:
        enriched_manifest, unresolvable_gaps = resolve_gaps(ir, manifest)
    except Exception as exc:
        logger.error(json.dumps({"message": "gap_resolution_failed", "error": str(exc)}))
        return _response(event, 500, {"error": f"Gap resolution failed: {exc}"})

    if unresolvable_gaps:
        logger.info(json.dumps({
            "message": "returning_gaps_to_orchestrator",
            "gap_count": len(unresolvable_gaps),
        }))
        return _response(event, 200, {
            "status": "gaps_found",
            "gaps": unresolvable_gaps,
        })

    # ---- Build prompt and invoke Bedrock ----
    model_id = os.environ.get("BEDROCK_MODEL_ID", "")
    if not model_id:
        return _response(event, 500, {"error": "BEDROCK_MODEL_ID environment variable is not set"})

    prompt = build_prompt(ir, enriched_manifest)

    logger.info(json.dumps({
        "message": "invoking_bedrock_for_hcl",
        "model_id": model_id,
        "service_count": len(ir.get("services", [])),
    }))

    try:
        hcl = _invoke_bedrock(bedrock, model_id, prompt)
    except Exception as exc:
        logger.error(json.dumps({"message": "bedrock_invocation_failed", "error": str(exc)}))
        return _response(event, 500, {"error": f"Bedrock invocation failed: {exc}"})

    # ---- Derive output S3 keys from the IR key ----
    # IR key convention: "diagrams/{stem}/ir.json"
    # We write:          "diagrams/{stem}/generated.tf"
    #                    "diagrams/{stem}/manifest_final.json"
    stem = ir_key.rsplit("/ir.json", 1)[0]  # e.g. "diagrams/my_arch"
    hcl_key = f"{stem}/generated.tf"
    final_manifest_key = f"{stem}/manifest_final.json"

    try:
        _put_text(s3, ir_bucket, hcl_key, hcl)
        _put_json(s3, ir_bucket, final_manifest_key, enriched_manifest)
    except Exception as exc:
        logger.error(json.dumps({"message": "s3_write_failed", "error": str(exc)}))
        return _response(event, 500, {"error": f"Failed to write artifacts to S3: {exc}"})

    logger.info(json.dumps({
        "message": "iac_generation_complete",
        "hcl_key": hcl_key,
        "manifest_key": final_manifest_key,
    }))

    return _response(event, 200, {
        "status": "success",
        "hcl_s3_key": hcl_key,
        "manifest_s3_key": final_manifest_key,
        "s3_bucket": ir_bucket,
    })
