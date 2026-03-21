#!/usr/bin/env python3
"""Creates a numbered Bedrock Agent version and creates/updates the named
production alias to point to it.

Uses boto3 bedrock-agent:create_agent_version directly (available since
boto3 1.35.x). The project originally used a CloudFormation workaround
because the API wasn't available — that workaround is no longer needed.

After promotion, writes agent-id and alias-id to SSM so OrchestratorStack
can read them at cdk synth time:
  /{project_name}/infra-agent/agent-id
  /{project_name}/infra-agent/alias-id

Writes GITHUB_OUTPUT-compatible lines:
  alias_id=<alias-id>

Exit codes:
  0 — promotion complete
  1 — failure
"""
import argparse
import os

import boto3


GITHUB_OUTPUT = os.environ.get("GITHUB_OUTPUT", "")


def set_output(key: str, value: str) -> None:
    print(f"  {key}={value}")
    if GITHUB_OUTPUT:
        with open(GITHUB_OUTPUT, "a") as f:
            f.write(f"{key}={value}\n")


def create_version(client, agent_id: str) -> str:
    """Creates a numbered agent version and returns the version number string."""
    response = client.create_agent_version(agentId=agent_id)
    return response["agentVersion"]["agentVersion"]


def find_alias(client, agent_id: str, alias_name: str):
    """Returns alias_id or None."""
    paginator = client.get_paginator("list_agent_aliases")
    for page in paginator.paginate(agentId=agent_id):
        for alias in page["agentAliasSummaries"]:
            if alias["agentAliasName"] == alias_name:
                return alias["agentAliasId"]
    return None


def write_to_ssm(alias_id: str, agent_id: str, project_name: str, region: str) -> None:
    """Persists agent-id and alias-id to SSM for OrchestratorStack lookup."""
    ssm = boto3.client("ssm", region_name=region)
    for param, value in [
        (f"/{project_name}/infra-agent/agent-id", agent_id),
        (f"/{project_name}/infra-agent/alias-id", alias_id),
    ]:
        ssm.put_parameter(Name=param, Value=value, Type="String", Overwrite=True)
        print(f"  SSM {param} = {value}")


def main():
    parser = argparse.ArgumentParser(
        description="Create a Bedrock agent version and update the production alias"
    )
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--alias-name", default="production")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--project-name", default="multi-agent-system")
    args = parser.parse_args()

    client = boto3.client("bedrock-agent", region_name=args.region)

    print(f"Creating new agent version for {args.agent_id}...")
    version = create_version(client, args.agent_id)
    print(f"Version {version} ready.")

    alias_id = find_alias(client, args.agent_id, args.alias_name)

    if alias_id:
        print(f"Updating alias '{args.alias_name}' ({alias_id}) → version {version}")
        client.update_agent_alias(
            agentId=args.agent_id,
            agentAliasId=alias_id,
            agentAliasName=args.alias_name,
            routingConfiguration=[{"agentVersion": version}],
        )
    else:
        print(f"Creating alias '{args.alias_name}' → version {version}")
        resp = client.create_agent_alias(
            agentId=args.agent_id,
            agentAliasName=args.alias_name,
            routingConfiguration=[{"agentVersion": version}],
        )
        alias_id = resp["agentAlias"]["agentAliasId"]

    print("Writing to SSM...")
    write_to_ssm(alias_id, args.agent_id, args.project_name, args.region)

    set_output("alias_id", alias_id)
    print("Promotion complete.")


if __name__ == "__main__":
    main()
