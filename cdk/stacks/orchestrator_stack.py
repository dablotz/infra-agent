import os
import re

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_bedrock as bedrock,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
)
from constructs import Construct

BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
BEDROCK_BASE_MODEL_ID = re.sub(r"^[a-z]{2}\.", "", BEDROCK_MODEL_ID)

# Infra-agent output bucket name follows this pattern (defined in InfraAgentStack).
# Referenced here without importing the stack to keep stacks decoupled.
INFRA_AGENT_NAME = "infra-agent"


class OrchestratorStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        project_name: str,
        infra_agent_id: str,
        infra_agent_alias_id: str,
        log_retention: logs.RetentionDays = logs.RetentionDays.ONE_MONTH,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account = self.account
        region = self.region

        here = os.path.dirname(__file__)
        repo_root = os.path.abspath(os.path.join(here, "..", ".."))
        bedrock_dir = os.path.join(repo_root, "agents", "orchestrator", "bedrock")
        lambda_dir = os.path.join(repo_root, "agents", "orchestrator", "lambda_functions")

        # Reference the IaC output bucket created by InfraAgentStack (no ownership transfer).
        iac_output_bucket = s3.Bucket.from_bucket_name(
            self,
            "IacOutputBucket",
            bucket_name=f"{INFRA_AGENT_NAME}-iac-output-{account}",
        )

        # ── Bedrock guardrail ────────────────────────────────────────────────
        guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=f"{project_name}-orchestrator-guardrail",
            blocked_input_messaging="I can only help with infrastructure management requests.",
            blocked_outputs_messaging="I cannot provide that type of response.",
            description="Guardrails for the orchestrator agent",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="HIGH", output_strength="HIGH", type="HATE"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="HIGH", output_strength="HIGH", type="INSULTS"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="HIGH", output_strength="HIGH", type="SEXUAL"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="HIGH", output_strength="HIGH", type="VIOLENCE"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="MEDIUM",
                        output_strength="NONE",
                        type="MISCONDUCT",
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="HIGH",
                        output_strength="NONE",
                        type="PROMPT_ATTACK",
                    ),
                ]
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        action="BLOCK", type="AWS_ACCESS_KEY"
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        action="BLOCK", type="AWS_SECRET_KEY"
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        action="BLOCK", type="PASSWORD"
                    ),
                ]
            ),
        )

        guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "GuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_arn,
            description="Production version",
        )

        # ── IAM role for orchestrator agent ──────────────────────────────────
        orchestrator_role = iam.Role(
            self,
            "OrchestratorRole",
            role_name=f"{project_name}-orchestrator-role",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock:{region}:{account}:agent/*"
                    },
                },
            ),
        )
        orchestrator_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:ListInferenceProfiles",
                    "bedrock:GetInferenceProfile",
                ],
                resources=[
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/{BEDROCK_MODEL_ID}",
                    f"arn:aws:bedrock:us-east-1::foundation-model/{BEDROCK_BASE_MODEL_ID}",
                    f"arn:aws:bedrock:us-west-2::foundation-model/{BEDROCK_BASE_MODEL_ID}",
                    f"arn:aws:bedrock:us-east-2::foundation-model/{BEDROCK_BASE_MODEL_ID}",
                ],
            )
        )
        orchestrator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeAgent"],
                resources=[
                    f"arn:aws:bedrock:{region}:{account}:agent-alias/{infra_agent_id}/*"
                ],
            )
        )
        # Bedrock internally calls these read APIs on behalf of the orchestrator's
        # execution role when associate_agent_collaborator is called, to verify the
        # collaborator alias exists. Without these, Bedrock returns a misleading
        # "no permissions to collaborate" ValidationException instead of an auth error.
        orchestrator_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:GetAgent",
                    "bedrock:GetAgentAlias",
                    "bedrock:ListAgents",
                    "bedrock:ListAgentAliases",
                ],
                resources=["*"],
            )
        )
        orchestrator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail.attr_guardrail_arn],
            )
        )

        # ── CloudWatch log groups ────────────────────────────────────────────
        logs.LogGroup(
            self,
            "OrchestratorLogGroup",
            log_group_name=f"/aws/bedrock/agents/{project_name}-orchestrator",
            retention=log_retention,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        log_group_doc_generator = logs.LogGroup(
            self,
            "LogGroupDocGenerator",
            log_group_name=f"/aws/lambda/{project_name}-doc-generator",
            retention=log_retention,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ── IAM role for doc generator Lambda ────────────────────────────────
        doc_generator_role = iam.Role(
            self,
            "DocGeneratorRole",
            role_name=f"{project_name}-doc-generator-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        doc_generator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/{BEDROCK_MODEL_ID}",
                    f"arn:aws:bedrock:us-east-1::foundation-model/{BEDROCK_BASE_MODEL_ID}",
                    f"arn:aws:bedrock:us-west-2::foundation-model/{BEDROCK_BASE_MODEL_ID}",
                    f"arn:aws:bedrock:us-east-2::foundation-model/{BEDROCK_BASE_MODEL_ID}",
                ],
            )
        )
        doc_generator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"{iac_output_bucket.bucket_arn}/generated/*"],
            )
        )
        doc_generator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[f"{iac_output_bucket.bucket_arn}/docs/*"],
            )
        )
        doc_generator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group_doc_generator.log_group_arn}:*"],
            )
        )

        # ── Doc generator Lambda ─────────────────────────────────────────────
        doc_generator = lambda_.Function(
            self,
            "DocGenerator",
            function_name=f"{project_name}-doc-generator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(lambda_dir, "doc_generator")
            ),
            role=doc_generator_role,
            timeout=cdk.Duration.seconds(300),
            memory_size=512,
            environment={
                "BEDROCK_MODEL_ID": BEDROCK_MODEL_ID,
                "OUTPUT_BUCKET": iac_output_bucket.bucket_name,
            },
            log_group=log_group_doc_generator,
        )

        # ── Orchestrator Bedrock agent ───────────────────────────────────────
        with open(os.path.join(bedrock_dir, "agent_instructions.txt")) as f:
            agent_instructions = f.read()

        with open(os.path.join(bedrock_dir, "generate_docs_schema.json")) as f:
            generate_docs_schema = f.read()

        # prepare_agent=False — the orchestrator must be prepared *after* the
        # collaborator is registered (done in a separate update or via the
        # promote script). Setting it to True here would race with collaborator
        # registration and cause a CloudFormation failure.
        orchestrator_agent = bedrock.CfnAgent(
            self,
            "OrchestratorAgent",
            agent_name=f"{project_name}-orchestrator",
            agent_resource_role_arn=orchestrator_role.role_arn,
            foundation_model=BEDROCK_MODEL_ID,
            instruction=agent_instructions,
            auto_prepare=False,
            agent_collaboration="SUPERVISOR",
            guardrail_configuration=bedrock.CfnAgent.GuardrailConfigurationProperty(
                guardrail_identifier=guardrail.attr_guardrail_id,
                guardrail_version=guardrail_version.attr_version,
            ),
            action_groups=[
                bedrock.CfnAgent.AgentActionGroupProperty(
                    action_group_name="GenerateDocs",
                    action_group_executor=bedrock.CfnAgent.ActionGroupExecutorProperty(
                        lambda_=doc_generator.function_arn
                    ),
                    api_schema=bedrock.CfnAgent.APISchemaProperty(
                        payload=generate_docs_schema
                    ),
                ),
            ],
        )

        # Allow Bedrock to invoke the doc generator Lambda
        doc_generator.add_permission(
            "BedrockInvokeDocGenerator",
            principal=iam.ServicePrincipal("bedrock.amazonaws.com"),
            source_arn=orchestrator_agent.attr_agent_arn,
        )

        # Collaborator registration and agent preparation are handled by
        # scripts/setup_orchestrator.py after this stack deploys.
        # AWS::Bedrock::AgentCollaborator is not available in CloudFormation
        # in all regions; boto3 associate_agent_collaborator is used instead.

        cdk.CfnOutput(
            self, "OrchestratorAgentId", value=orchestrator_agent.attr_agent_id
        )
        cdk.CfnOutput(
            self, "OrchestratorAgentArn", value=orchestrator_agent.attr_agent_arn
        )
