#!/usr/bin/env python3
"""Smoke test for the infra-agent.

Invokes the agent via a specified alias and verifies it returns a non-empty
response without error. Designed to run against TSTALIASID (DRAFT) after a
terraform apply, before promoting DRAFT to a numbered production version.

Exit codes:
  0 — agent responded successfully
  1 — invocation error or empty response
"""
import argparse
import sys
import uuid

import boto3
from botocore.config import Config


def main():
    parser = argparse.ArgumentParser(description="Smoke test the infra-agent")
    parser.add_argument("--agent-id", required=True, help="Bedrock agent ID")
    parser.add_argument(
        "--alias-id",
        default="TSTALIASID",
        help="Agent alias ID (default: TSTALIASID which points to DRAFT)",
    )
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    # The full generation pipeline can take 30-90s; raise the read timeout.
    client = boto3.client(
        "bedrock-agent-runtime",
        region_name=args.region,
        config=Config(read_timeout=300, connect_timeout=10),
    )
    session_id = str(uuid.uuid4())

    print(f"Smoke test: agent={args.agent_id} alias={args.alias_id}")

    try:
        # endSession omitted — setting it True causes Bedrock to return a
        # session termination message instead of the agent's actual response.
        response = client.invoke_agent(
            agentId=args.agent_id,
            agentAliasId=args.alias_id,
            sessionId=session_id,
            inputText="Generate a minimal S3 bucket resource in Terraform.",
        )

        output_text = ""
        for event in response["completion"]:
            if "chunk" in event:
                output_text += event["chunk"]["bytes"].decode("utf-8")

        if not output_text:
            print("FAIL: agent returned an empty response")
            sys.exit(1)

        print(f"PASS: agent responded ({len(output_text)} chars)")
        print(f"Preview: {output_text[:300]}")
        sys.exit(0)

    except client.exceptions.ResourceNotFoundException as e:
        print(f"FAIL: agent or alias not found — {e}")
        sys.exit(1)
    except Exception as e:
        print(f"FAIL: invocation error — {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
