#!/usr/bin/env python3
"""Promotes the infra-agent staging version to the production alias.

Reads the agent version that the staging alias currently points to (created
by CloudFormation when InfraAgentStack deployed), then creates or updates the
production alias to point to that same version.

This avoids calling boto3 create_agent_version, which does not exist in the
boto3 Bedrock Agent API. Instead, CDK's CfnAgentAlias (without
routing_configuration) handles version creation automatically during deploy.

After promotion, writes agent-id and alias-id to SSM so OrchestratorStack
can read them:
  /{project_name}/infra-agent/agent-id
  /{project_name}/infra-agent/alias-id

Writes GITHUB_OUTPUT-compatible lines:
  alias_id=<production-alias-id>

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


def get_version_from_alias(client, agent_id: str, alias_id: str) -> str:
    """Returns the agent version number that the given alias points to."""
    response = client.get_agent_alias(agentId=agent_id, agentAliasId=alias_id)
    routing = response["agentAlias"].get("routingConfiguration", [])
    if not routing:
        raise ValueError(f"Staging alias {alias_id} has no routing configuration")
    return routing[0]["agentVersion"]


def find_alias(client, agent_id: str, alias_name: str):
    """Returns alias_id if the named alias exists, else None."""
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
        description="Promote staging version to the production alias"
    )
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--staging-alias-id", required=True,
                        help="Alias ID of the CDK-managed staging alias")
    parser.add_argument("--alias-name", default="production",
                        help="Name of the production alias to create or update")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--project-name", default="multi-agent-system")
    args = parser.parse_args()

    client = boto3.client("bedrock-agent", region_name=args.region)

    print(f"Reading version from staging alias {args.staging_alias_id}...")
    version = get_version_from_alias(client, args.agent_id, args.staging_alias_id)
    print(f"Staging alias points to version {version}.")

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
