import json
import boto3
import os

sfn = boto3.client("stepfunctions")


def lambda_handler(event, context):
    action_group = event.get("actionGroup", "")
    api_path = event.get("apiPath", "")
    parameters = event.get("parameters", [])

    user_request = next(
        (p["value"] for p in parameters if p["name"] == "user_request"), ""
    )
    iac_type = next(
        (p["value"] for p in parameters if p["name"] == "iac_type"), "terraform"
    )

    execution_input = {"user_request": user_request, "iac_type": iac_type}

    response = sfn.start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        input=json.dumps(execution_input),
    )

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
                            "message": "IaC generation pipeline started successfully",
                            "execution_arn": response["executionArn"],
                            "iac_type": iac_type,
                        }
                    )
                }
            },
        },
    }
