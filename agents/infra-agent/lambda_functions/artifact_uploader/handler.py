import json
import logging
import boto3
import os
from datetime import datetime, timezone
import uuid

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


def lambda_handler(event, context, s3_client=None):
    s3 = s3_client or boto3.client("s3")

    props = _get_props(event)
    generated_code = _prop(props, "generated_code")
    iac_type = _prop(props, "iac_type", "terraform")
    user_request = _prop(props, "user_request")
    validation_status = _prop(props, "validation_status", "unknown")
    security_status = _prop(props, "security_status", "unknown")

    if not generated_code:
        return _response(event, 400, {"error": "generated_code is required"})

    bucket = os.environ["OUTPUT_BUCKET"]
    request_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    file_ext = ".tf" if iac_type == "terraform" else ".yaml"
    key = f"generated/{request_id}/{timestamp}{file_ext}"

    metadata = {
        "user-request": user_request[:1024],
        "iac-type": iac_type,
        "validation-status": validation_status,
        "security-status": security_status,
        "request-id": request_id,
    }

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=generated_code.encode("utf-8"),
        ContentType="text/plain",
        Metadata=metadata,
    )

    pipeline_summary = {
        "request_id": request_id,
        "user_request": user_request,
        "iac_type": iac_type,
        "validation_status": validation_status,
        "security_status": security_status,
        "s3_uri": f"s3://{bucket}/{key}",
    }
    metadata_key = f"generated/{request_id}/{timestamp}-metadata.json"
    s3.put_object(
        Bucket=bucket,
        Key=metadata_key,
        Body=json.dumps(pipeline_summary, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    logger.info(json.dumps({
        "message": "artifact_uploaded",
        "request_id": request_id,
        "s3_uri": f"s3://{bucket}/{key}",
        "validation_status": validation_status,
        "security_status": security_status,
    }))

    return _response(event, 200, {
        "s3_uri": f"s3://{bucket}/{key}",
        "s3_key": key,
        "s3_bucket": bucket,
        "request_id": request_id,
    })
