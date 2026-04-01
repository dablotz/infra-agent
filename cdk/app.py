#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.shared_stack import SharedStack
from stacks.infra_agent_stack import InfraAgentStack
from stacks.orchestrator_stack import OrchestratorStack
from stacks.diagram_pipeline_stack import DiagramPipelineStack

app = cdk.App()

env = cdk.Environment(region=os.environ.get("AWS_REGION", "us-east-1"))

project_name = app.node.try_get_context("project_name") or "multi-agent-system"
github_repo = app.node.try_get_context("github_repo") or ""
create_oidc_provider = app.node.try_get_context("create_github_oidc_provider") != "false"

# infra_agent_id and infra_agent_alias_id are only required when deploying
# OrchestratorStack. Pass them via -c flags after InfraAgentStack is deployed
# and promote_agent.py has run:
#   cdk deploy OrchestratorStack -c infra_agent_id=... -c infra_agent_alias_id=...
infra_agent_id = app.node.try_get_context("infra_agent_id") or ""
infra_agent_alias_id = app.node.try_get_context("infra_agent_alias_id") or ""

# orchestrator_agent_id and orchestrator_alias_id are required when deploying
# DiagramPipelineStack. Pass them via -c flags after OrchestratorStack deploys:
#   cdk deploy DiagramPipelineStack \
#       -c orchestrator_agent_id=... -c orchestrator_alias_id=...
orchestrator_agent_id = app.node.try_get_context("orchestrator_agent_id") or ""
orchestrator_alias_id = app.node.try_get_context("orchestrator_alias_id") or ""

SharedStack(
    app,
    "SharedStack",
    project_name=project_name,
    github_repo=github_repo,
    create_oidc_provider=create_oidc_provider,
    env=env,
)

infra_agent_stack = InfraAgentStack(
    app,
    "InfraAgentStack",
    project_name=project_name,
    env=env,
)

OrchestratorStack(
    app,
    "OrchestratorStack",
    project_name=project_name,
    infra_agent_id=infra_agent_id,
    infra_agent_alias_id=infra_agent_alias_id,
    env=env,
)

DiagramPipelineStack(
    app,
    "DiagramPipelineStack",
    project_name=project_name,
    orchestrator_agent_id=orchestrator_agent_id,
    orchestrator_alias_id=orchestrator_alias_id,
    iac_agent_role=infra_agent_stack.iac_agent_role,
    env=env,
)

app.synth()
