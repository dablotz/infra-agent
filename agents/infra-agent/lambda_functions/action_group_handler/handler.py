import json
import time
import boto3
import os

MAX_REQUEST_LENGTH = 4096
POLL_INTERVAL_SECONDS = 5
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}


def lambda_handler(event, context, sfn_client=None):
    sfn = sfn_client or boto3.client("stepfunctions")

    action_group = event.get("actionGroup", "")
    api_path = event.get("apiPath", "")

    # requestBody parameters (OpenAPI requestBody schema)
    properties = (
        event.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("properties", [])
    )
    # Fall back to top-level parameters (path/query params)
    parameters = properties or event.get("parameters", [])

    user_request = next(
        (p["value"] for p in parameters if p["name"] == "user_request"), ""
    )
    iac_type = next(
        (p["value"] for p in parameters if p["name"] == "iac_type"), "terraform"
    )

    if not user_request:
        return _error_response(action_group, api_path, 400, "user_request is required")

    if len(user_request) > MAX_REQUEST_LENGTH:
        return _error_response(
            action_group,
            api_path,
            400,
            f"user_request exceeds maximum length of {MAX_REQUEST_LENGTH} characters",
        )

    execution_input = {
        "user_request": user_request,
        "iac_type": iac_type,
        "retry_count": 0,
    }

    response = sfn.start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        input=json.dumps(execution_input),
    )
    execution_arn = response["executionArn"]

    while True:
        status_response = sfn.describe_execution(executionArn=execution_arn)
        status = status_response["status"]

        if status == "SUCCEEDED":
            output = json.loads(status_response["output"])
            return {
                "messageVersion": "1.0",
                "response": {
                    "actionGroup": action_group,
                    "apiPath": api_path,
                    "httpMethod": "POST",
                    "httpStatusCode": 200,
                    "responseBody": {
                        "application/json": {
                            "body": json.dumps(
                                {
                                    "message": "IaC generated successfully",
                                    "s3_uri": output.get("s3_uri"),
                                    "s3_bucket": output.get("s3_bucket"),
                                    "s3_key": output.get("s3_key"),
                                    "iac_type": output.get("iac_type"),
                                    "validation_status": output.get("validation_status"),
                                    "security_status": output.get("security_status"),
                                }
                            )
                        }
                    },
                },
            }

        if status in ("FAILED", "TIMED_OUT", "ABORTED"):
            cause = status_response.get("cause", "Unknown error")
            return _error_response(
                action_group, api_path, 500, f"Pipeline {status.lower()}: {cause}"
            )

        if context.get_remaining_time_in_millis() < 15000:
            return _error_response(
                action_group, api_path, 504, "Pipeline did not complete within the allowed time"
            )

        time.sleep(POLL_INTERVAL_SECONDS)


def _error_response(action_group, api_path, status_code, message):
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": action_group,
            "apiPath": api_path,
            "httpMethod": "POST",
            "httpStatusCode": status_code,
            "responseBody": {
                "application/json": {"body": json.dumps({"error": message})}
            },
        },
    }
