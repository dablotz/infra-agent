import json
import boto3
import os

bedrock = boto3.client("bedrock-runtime")


def lambda_handler(event, context):
    user_request = event.get("user_request", "")
    iac_type = event.get("iac_type", "terraform")

    prompt = f"""Generate {iac_type} code for the following infrastructure request:

{user_request}

Requirements:
- Generate complete, syntactically correct code
- Include all necessary providers and variables
- Follow best practices for security and maintainability
- Output ONLY the code, no explanations

Code:"""

    response = bedrock.invoke_model(
        modelId=os.environ["BEDROCK_MODEL_ID"],
        body=json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            }
        ),
    )

    result = json.loads(response["body"].read())
    generated_code = result["content"][0]["text"]

    return {
        "statusCode": 200,
        "user_request": user_request,
        "iac_type": iac_type,
        "generated_code": generated_code,
    }
