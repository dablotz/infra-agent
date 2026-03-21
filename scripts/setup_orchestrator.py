#!/usr/bin/env python3
"""Registers the infra-agent as a collaborator on the orchestrator agent and
prepares the orchestrator.

CloudFormation does not support AWS::Bedrock::AgentCollaborator in all
regions, so this step is handled via boto3 after OrchestratorStack deploys.

Must be run after both InfraAgentStack and OrchestratorStack are deployed.

Exit codes:
  0 — success
  1 — failure
"""
import argparse
import time

import boto3

COLLABORATION_INSTRUCTION = (
    "Use this agent for all Infrastructure as Code generation requests. "
    "It generates validated, security-scanned Terraform, CloudFormation, or CDK "
    "from natural language descriptions and returns the S3 URI of the generated artifact."
)


def get_ssm(name: str, region: str) -> str:
    ssm = boto3.client("ssm", region_name=region)
    return ssm.get_parameter(Name=name)["Parameter"]["Value"]


def find_collaborator(client, orchestrator_id: str, name: str):
    """Returns the collaborator ID if already registered, else None."""
    paginator = client.get_paginator("list_agent_collaborators")
    for page in paginator.paginate(agentId=orchestrator_id, agentVersion="DRAFT"):
        for c in page["agentCollaboratorSummaries"]:
            if c["collaboratorName"] == name:
                return c["collaboratorId"]
    return None


def wait_for_prepared(client, agent_id: str, timeout: int = 120) -> None:
    """Polls until the agent reaches PREPARED status."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get_agent(agentId=agent_id)["agent"]["agentStatus"]
        if status == "PREPARED":
            return
        if status in ("FAILED", "NOT_PREPARED"):
            raise RuntimeError(f"Agent preparation failed with status: {status}")
        time.sleep(5)
    raise TimeoutError(f"Agent {agent_id} did not reach PREPARED within {timeout}s")


def main():
    parser = argparse.ArgumentParser(
        description="Register infra-agent collaborator and prepare orchestrator"
    )
    parser.add_argument("--orchestrator-id", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--project-name", default="multi-agent-system")
    args = parser.parse_args()

    client = boto3.client("bedrock-agent", region_name=args.region)

    infra_agent_id = get_ssm(f"/{args.project_name}/infra-agent/agent-id", args.region)
    infra_agent_alias_id = get_ssm(f"/{args.project_name}/infra-agent/alias-id", args.region)
    alias_arn = (
        f"arn:aws:bedrock:{args.region}:"
        f"{boto3.client('sts').get_caller_identity()['Account']}:"
        f"agent-alias/{infra_agent_id}/{infra_agent_alias_id}"
    )

    existing = find_collaborator(client, args.orchestrator_id, "InfraAgent")

    if existing:
        print(f"Updating collaborator {existing}...")
        client.update_agent_collaborator(
            agentId=args.orchestrator_id,
            agentVersion="DRAFT",
            collaboratorId=existing,
            agentDescriptor={"aliasArn": alias_arn},
            collaboratorName="InfraAgent",
            collaborationInstruction=COLLABORATION_INSTRUCTION,
            relayConversationHistory="TO_COLLABORATOR",
        )
    else:
        print("Associating infra-agent as collaborator...")
        client.associate_agent_collaborator(
            agentId=args.orchestrator_id,
            agentVersion="DRAFT",
            agentDescriptor={"aliasArn": alias_arn},
            collaboratorName="InfraAgent",
            collaborationInstruction=COLLABORATION_INSTRUCTION,
            relayConversationHistory="TO_COLLABORATOR",
        )

    print("Preparing orchestrator agent...")
    client.prepare_agent(agentId=args.orchestrator_id)
    wait_for_prepared(client, args.orchestrator_id)
    print("Orchestrator ready.")


if __name__ == "__main__":
    main()
