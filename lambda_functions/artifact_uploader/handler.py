import json
import boto3
import os
from datetime import datetime

s3 = boto3.client("s3")


def lambda_handler(event, context):
    generated_code = event.get("generated_code", "")
    iac_type = event.get("iac_type", "terraform")
    user_request = event.get("user_request", "")

    bucket = os.environ["OUTPUT_BUCKET"]
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    file_ext = ".tf" if iac_type == "terraform" else ".yaml"
    key = f"generated/{timestamp}{file_ext}"

    metadata = {
        "user-request": user_request[:1024],
        "iac-type": iac_type,
        "validation-status": event.get("validation_status", "unknown"),
        "security-status": event.get("security_status", "unknown"),
    }

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=generated_code.encode("utf-8"),
        ContentType="text/plain",
        Metadata=metadata,
    )

    metadata_key = f"generated/{timestamp}-metadata.json"
    s3.put_object(
        Bucket=bucket,
        Key=metadata_key,
        Body=json.dumps(event, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    return {
        **event,
        "s3_bucket": bucket,
        "s3_key": key,
        "s3_metadata_key": metadata_key,
        "s3_uri": f"s3://{bucket}/{key}",
    }
