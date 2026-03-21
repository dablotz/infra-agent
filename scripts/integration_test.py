#!/usr/bin/env python3
"""Integration tests for the infra-agent.

Invokes the agent against TSTALIASID (DRAFT) with multiple IaC generation
scenarios. Each test confirms the full pipeline ran by asserting that an S3
URI — the artifact_uploader's definitive end-to-end signal — appears in the
response and that the corresponding S3 object exists with correct metadata.

Run after every CDK deploy of InfraAgentStack, before promoting DRAFT to a
numbered production version via scripts/promote_agent.py.

Exit codes:
  0 — all tests passed
  1 — one or more tests failed
"""
import argparse
import re
import sys
import uuid

import boto3


# S3 URI produced by artifact_uploader:
#   s3://{bucket}/generated/{uuid}/{timestamp}.{tf|yaml}
# timestamp format: YYYYMMDD-HHMMSS
_S3_URI_RE = re.compile(
    r"s3://(?P<bucket>[^\s/]+)/(?P<key>generated/[a-f0-9-]+/\d{8}-\d{6}\.(tf|yaml))",
    re.IGNORECASE,
)

# Each test case: a prompt and the IaC type it requests.
# iac_type must match artifact_uploader's classification ("terraform" → .tf,
# anything else → .yaml). The agent infers iac_type from the prompt, so keep
# prompts unambiguous.
TEST_CASES = [
    {
        "name": "terraform_s3_bucket",
        "description": "Terraform — S3 bucket with versioning",
        "prompt": (
            "Generate a minimal Terraform configuration for an S3 bucket "
            "with versioning enabled."
        ),
        "expected_ext": ".tf",
    },
    {
        "name": "cloudformation_dynamodb",
        "description": "CloudFormation — DynamoDB table with on-demand billing",
        "prompt": (
            "Generate a CloudFormation template for a DynamoDB table with a "
            "string partition key named 'id' and on-demand billing mode."
        ),
        "expected_ext": ".yaml",
    },
    {
        "name": "terraform_sqs_dlq",
        "description": "Terraform — SQS queue with dead-letter queue",
        "prompt": (
            "Generate Terraform for an SQS queue with a dead-letter queue. "
            "Include the redrive policy connecting them."
        ),
        "expected_ext": ".tf",
    },
]


def invoke_agent(client, agent_id: str, alias_id: str, prompt: str) -> str:
    """Invokes the agent and returns the full concatenated response text."""
    response = client.invoke_agent(
        agentId=agent_id,
        agentAliasId=alias_id,
        sessionId=str(uuid.uuid4()),
        inputText=prompt,
        endSession=True,
    )
    return "".join(
        event["chunk"]["bytes"].decode("utf-8")
        for event in response["completion"]
        if "chunk" in event
    )


def verify_s3_object(s3_client, bucket: str, key: str, expected_ext: str) -> tuple[bool, str]:
    """Confirms the artifact exists in S3 and has the expected metadata."""
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
    except s3_client.exceptions.NoSuchKey:
        return False, f"S3 object not found: s3://{bucket}/{key}"
    except Exception as e:
        return False, f"S3 head_object failed: {e}"

    if not key.endswith(expected_ext):
        return False, f"S3 key has wrong extension (got {key.split('.')[-1]}, expected {expected_ext.lstrip('.')})"

    meta = head.get("Metadata", {})
    for required_key in ("validation-status", "security-status", "iac-type"):
        if required_key not in meta:
            return False, f"S3 object missing metadata key: {required_key}"

    return True, f"validation={meta['validation-status']} security={meta['security-status']}"


def run_test(runtime_client, s3_client, agent_id: str, alias_id: str, test: dict) -> tuple[bool, str]:
    """Runs one test case. Returns (passed, detail)."""
    print(f"\n[{test['name']}] {test['description']}")
    print(f"  Prompt: {test['prompt'][:90]}...")

    try:
        response = invoke_agent(runtime_client, agent_id, alias_id, test["prompt"])
    except Exception as e:
        return False, f"invoke_agent failed: {e}"

    if not response:
        return False, "Agent returned an empty response"

    # The S3 URI is the definitive signal that all four action groups ran.
    match = _S3_URI_RE.search(response)
    if not match:
        preview = response[:300].replace("\n", " ")
        return False, f"No S3 URI in response. Preview: {preview}"

    bucket = match.group("bucket")
    key = match.group("key")
    print(f"  S3 URI: s3://{bucket}/{key}")

    # Confirm the object actually exists and has correct metadata.
    ok, detail = verify_s3_object(s3_client, bucket, key, test["expected_ext"])
    if not ok:
        return False, detail

    print(f"  PASS — {detail} ({len(response)} char response)")
    return True, "OK"


def main():
    parser = argparse.ArgumentParser(description="Integration tests for the infra-agent")
    parser.add_argument("--agent-id", required=True, help="Bedrock agent ID")
    parser.add_argument(
        "--alias-id",
        default="TSTALIASID",
        help="Agent alias ID (default: TSTALIASID → DRAFT)",
    )
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    runtime = boto3.client("bedrock-agent-runtime", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)

    print(f"Integration tests: agent={args.agent_id} alias={args.alias_id}")
    print(f"Running {len(TEST_CASES)} test cases...\n")

    results = []
    for test in TEST_CASES:
        passed, detail = run_test(runtime, s3, args.agent_id, args.alias_id, test)
        results.append((test["name"], passed, detail))

    print("\n── Results " + "─" * 50)
    passed_count = sum(1 for _, p, _ in results if p)
    for name, passed, detail in results:
        status = "PASS" if passed else "FAIL"
        suffix = f": {detail}" if not passed else ""
        print(f"  [{status}] {name}{suffix}")

    print(f"\n{passed_count}/{len(results)} tests passed")

    if passed_count < len(results):
        print("Integration tests FAILED — production alias will not be promoted.")
        sys.exit(1)

    print("Integration tests PASSED — ready to promote to production.")
    sys.exit(0)


if __name__ == "__main__":
    main()
