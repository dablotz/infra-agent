import json
import os

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_bedrock as bedrock,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_ssm as ssm,
    custom_resources as cr,
)
from constructs import Construct

from constants import BEDROCK_MODEL_ID, BEDROCK_BASE_MODEL_ID

# Resource name prefix for all infra-agent resources — matches existing naming.
AGENT_NAME = "infra-agent"


class InfraAgentStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        project_name: str,
        log_retention: logs.RetentionDays = logs.RetentionDays.ONE_MONTH,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account = self.account
        region = self.region

        # Resolve paths relative to this file so they work regardless of the
        # working directory CDK is invoked from.
        here = os.path.dirname(__file__)
        repo_root = os.path.abspath(os.path.join(here, "..", ".."))
        lambda_dir = os.path.join(repo_root, "agents", "infra-agent", "lambda_functions")
        bedrock_dir = os.path.join(repo_root, "agents", "infra-agent", "bedrock")
        layers_dir = os.path.join(repo_root, "shared", "lambda_layers")

        # ── S3: IaC output bucket ────────────────────────────────────────────
        iac_output_bucket = s3.Bucket(
            self,
            "IacOutput",
            bucket_name=f"{AGENT_NAME}-iac-output-{account}",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            event_bridge_enabled=True,  # forwards S3 events to EventBridge
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ── Lambda layers ────────────────────────────────────────────────────
        # Both zips are built by `make package-layers` before `cdk deploy`.
        terraform_layer = lambda_.LayerVersion(
            self,
            "TerraformToolsLayer",
            code=lambda_.Code.from_asset(
                os.path.join(layers_dir, "terraform_tools.zip")
            ),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            layer_version_name=f"{AGENT_NAME}-terraform-tools",
            description="Terraform and tflint binaries for IaC validation",
        )

        security_layer = lambda_.LayerVersion(
            self,
            "SecurityToolsLayer",
            code=lambda_.Code.from_asset(
                os.path.join(layers_dir, "security_tools.zip")
            ),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            layer_version_name=f"{AGENT_NAME}-security-tools",
            description="Checkov for security scanning",
        )

        # ── CloudWatch log groups ────────────────────────────────────────────
        # Created explicitly so retention is set and the groups survive a
        # Lambda function replacement.
        log_group_generator = logs.LogGroup(
            self,
            "LogGroupGenerator",
            log_group_name=f"/aws/lambda/{AGENT_NAME}-code-generator",
            retention=log_retention,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        log_group_validator = logs.LogGroup(
            self,
            "LogGroupValidator",
            log_group_name=f"/aws/lambda/{AGENT_NAME}-validator",
            retention=log_retention,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        log_group_scanner = logs.LogGroup(
            self,
            "LogGroupScanner",
            log_group_name=f"/aws/lambda/{AGENT_NAME}-security-scanner",
            retention=log_retention,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        log_group_uploader = logs.LogGroup(
            self,
            "LogGroupUploader",
            log_group_name=f"/aws/lambda/{AGENT_NAME}-artifact-uploader",
            retention=log_retention,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ── IAM roles ────────────────────────────────────────────────────────
        lambda_trust = iam.ServicePrincipal("lambda.amazonaws.com")

        # Code generator: needs Bedrock InvokeModel + CloudWatch logs
        generator_role = iam.Role(
            self,
            "LambdaGeneratorRole",
            role_name=f"{AGENT_NAME}-lambda-generator-role",
            assumed_by=lambda_trust,
        )
        generator_role.add_to_policy(
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
        generator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group_generator.log_group_arn}:*"],
            )
        )

        # Validator: CloudWatch logs only (runs terraform/tflint locally)
        validator_role = iam.Role(
            self,
            "LambdaValidatorRole",
            role_name=f"{AGENT_NAME}-lambda-validator-role",
            assumed_by=lambda_trust,
        )
        validator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group_validator.log_group_arn}:*"],
            )
        )

        # Security scanner: CloudWatch logs only (runs checkov locally)
        scanner_role = iam.Role(
            self,
            "LambdaScannerRole",
            role_name=f"{AGENT_NAME}-lambda-scanner-role",
            assumed_by=lambda_trust,
        )
        scanner_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group_scanner.log_group_arn}:*"],
            )
        )

        # Artifact uploader: S3 PutObject + CloudWatch logs
        uploader_role = iam.Role(
            self,
            "LambdaUploaderRole",
            role_name=f"{AGENT_NAME}-lambda-uploader-role",
            assumed_by=lambda_trust,
        )
        uploader_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[f"{iac_output_bucket.bucket_arn}/*"],
            )
        )
        uploader_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group_uploader.log_group_arn}:*"],
            )
        )

        # ── Lambda functions ─────────────────────────────────────────────────
        # CDK packages each handler directory into a zip automatically.

        code_generator = lambda_.Function(
            self,
            "CodeGenerator",
            function_name=f"{AGENT_NAME}-code-generator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(lambda_dir, "code_generator")
            ),
            role=generator_role,
            timeout=cdk.Duration.seconds(300),
            memory_size=512,
            environment={"BEDROCK_MODEL_ID": BEDROCK_MODEL_ID},
            log_group=log_group_generator,
        )

        validator = lambda_.Function(
            self,
            "Validator",
            function_name=f"{AGENT_NAME}-validator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(lambda_dir, "validator")),
            role=validator_role,
            timeout=cdk.Duration.seconds(300),
            memory_size=1024,
            ephemeral_storage_size=cdk.Size.mebibytes(2048),
            layers=[terraform_layer],
            log_group=log_group_validator,
        )

        security_scanner = lambda_.Function(
            self,
            "SecurityScanner",
            function_name=f"{AGENT_NAME}-security-scanner",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(lambda_dir, "security_scanner")
            ),
            role=scanner_role,
            timeout=cdk.Duration.seconds(300),
            memory_size=1024,
            layers=[security_layer],
            log_group=log_group_scanner,
        )

        artifact_uploader = lambda_.Function(
            self,
            "ArtifactUploader",
            function_name=f"{AGENT_NAME}-artifact-uploader",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(lambda_dir, "artifact_uploader")
            ),
            role=uploader_role,
            timeout=cdk.Duration.seconds(60),
            memory_size=256,
            environment={"OUTPUT_BUCKET": iac_output_bucket.bucket_name},
            log_group=log_group_uploader,
        )

        # ── Bedrock guardrail ────────────────────────────────────────────────
        guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=f"{AGENT_NAME}-guardrail",
            blocked_input_messaging=(
                "I can only help with Infrastructure as Code generation requests. "
                "Please provide a valid infrastructure description."
            ),
            blocked_outputs_messaging=(
                "I cannot generate that type of content. "
                "Please request valid infrastructure code."
            ),
            description="Guardrails for IaC generation agent",
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
                        output_strength="MEDIUM",
                        type="MISCONDUCT",
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="HIGH",
                        output_strength="NONE",
                        type="PROMPT_ATTACK",
                    ),
                ]
            ),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="off-topic",
                        definition=(
                            "Non-infrastructure topics including personal advice, "
                            "entertainment, general knowledge questions, or anything "
                            "unrelated to cloud infrastructure."
                        ),
                        examples=[
                            "Tell me a joke",
                            "What's the weather",
                            "Write me a story",
                            "Help me with my homework",
                        ],
                        type="DENY",
                    )
                ]
            ),
            word_policy_config=bedrock.CfnGuardrail.WordPolicyConfigProperty(
                managed_word_lists_config=[
                    bedrock.CfnGuardrail.ManagedWordsConfigProperty(type="PROFANITY")
                ],
                words_config=[
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="ignore previous instructions"
                    ),
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="disregard all previous"
                    ),
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="forget your instructions"
                    ),
                ],
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
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        action="ANONYMIZE", type="EMAIL"
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        action="ANONYMIZE", type="PHONE"
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

        # ── Bedrock agent IAM role ───────────────────────────────────────────
        bedrock_agent_role = iam.Role(
            self,
            "BedrockAgentRole",
            role_name=f"{AGENT_NAME}-bedrock-agent-role",
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
        bedrock_agent_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowModelAndInferenceProfileAccess",
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
        bedrock_agent_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowMarketplaceSubscription",
                actions=["aws-marketplace:ViewSubscriptions"],
                resources=["*"],
                conditions={
                    "StringEquals": {"aws:CalledViaLast": "bedrock.amazonaws.com"}
                },
            )
        )
        bedrock_agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail.attr_guardrail_arn],
            )
        )

        # ── Bedrock agent ────────────────────────────────────────────────────
        with open(os.path.join(bedrock_dir, "agent_instructions.txt")) as f:
            agent_instructions = f.read()

        with open(os.path.join(bedrock_dir, "generate_iac_schema.json")) as f:
            generate_schema = f.read()

        with open(os.path.join(bedrock_dir, "validate_iac_schema.json")) as f:
            validate_schema = f.read()

        with open(os.path.join(bedrock_dir, "scan_iac_schema.json")) as f:
            scan_schema = f.read()

        with open(os.path.join(bedrock_dir, "upload_iac_schema.json")) as f:
            upload_schema = f.read()

        with open(os.path.join(bedrock_dir, "process_diagram_schema.json")) as f:
            process_diagram_schema = f.read()

        # ── IAM + Lambda: iac_agent (diagram-to-Terraform pipeline) ─────────
        log_group_iac_agent = logs.LogGroup(
            self,
            "LogGroupIacAgent",
            log_group_name=f"/aws/lambda/{AGENT_NAME}-iac-agent",
            retention=log_retention,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # The iac_agent role intentionally starts with no S3 access: the
        # diagrams bucket is created by DiagramPipelineStack, which calls
        # diagrams_bucket.grant_read_write(iac_agent_role) after this stack
        # deploys. Bedrock InvokeModel access mirrors the code generator role.
        iac_agent_role = iam.Role(
            self,
            "LambdaIacAgentRole",
            role_name=f"{AGENT_NAME}-lambda-iac-agent-role",
            assumed_by=lambda_trust,
        )
        iac_agent_role.add_to_policy(
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
        iac_agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group_iac_agent.log_group_arn}:*"],
            )
        )

        iac_agent_fn = lambda_.Function(
            self,
            "IacAgentFn",
            function_name=f"{AGENT_NAME}-iac-agent",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(lambda_dir, "iac_agent")
            ),
            role=iac_agent_role,
            timeout=cdk.Duration.seconds(300),
            memory_size=512,
            environment={"BEDROCK_MODEL_ID": BEDROCK_MODEL_ID},
            log_group=log_group_iac_agent,
        )

        # Expose role so DiagramPipelineStack can grant S3 bucket access
        self.iac_agent_role: iam.IRole = iac_agent_role

        cfn_agent = bedrock.CfnAgent(
            self,
            "IacAgent",
            agent_name=AGENT_NAME,
            agent_resource_role_arn=bedrock_agent_role.role_arn,
            foundation_model=BEDROCK_MODEL_ID,
            instruction=agent_instructions,
            auto_prepare=True,
            guardrail_configuration=bedrock.CfnAgent.GuardrailConfigurationProperty(
                guardrail_identifier=guardrail.attr_guardrail_id,
                guardrail_version=guardrail_version.attr_version,
            ),
            action_groups=[
                bedrock.CfnAgent.AgentActionGroupProperty(
                    action_group_name="ProcessDiagram",
                    action_group_executor=bedrock.CfnAgent.ActionGroupExecutorProperty(
                        lambda_=iac_agent_fn.function_arn
                    ),
                    api_schema=bedrock.CfnAgent.APISchemaProperty(
                        payload=process_diagram_schema
                    ),
                ),
                bedrock.CfnAgent.AgentActionGroupProperty(
                    action_group_name="GenerateIaC",
                    action_group_executor=bedrock.CfnAgent.ActionGroupExecutorProperty(
                        lambda_=code_generator.function_arn
                    ),
                    api_schema=bedrock.CfnAgent.APISchemaProperty(
                        payload=generate_schema
                    ),
                ),
                bedrock.CfnAgent.AgentActionGroupProperty(
                    action_group_name="ValidateIaC",
                    action_group_executor=bedrock.CfnAgent.ActionGroupExecutorProperty(
                        lambda_=validator.function_arn
                    ),
                    api_schema=bedrock.CfnAgent.APISchemaProperty(
                        payload=validate_schema
                    ),
                ),
                bedrock.CfnAgent.AgentActionGroupProperty(
                    action_group_name="ScanIaC",
                    action_group_executor=bedrock.CfnAgent.ActionGroupExecutorProperty(
                        lambda_=security_scanner.function_arn
                    ),
                    api_schema=bedrock.CfnAgent.APISchemaProperty(
                        payload=scan_schema
                    ),
                ),
                bedrock.CfnAgent.AgentActionGroupProperty(
                    action_group_name="UploadIaC",
                    action_group_executor=bedrock.CfnAgent.ActionGroupExecutorProperty(
                        lambda_=artifact_uploader.function_arn
                    ),
                    api_schema=bedrock.CfnAgent.APISchemaProperty(
                        payload=upload_schema
                    ),
                ),
            ],
        )

        # Allow Bedrock to invoke each Lambda action group executor
        for fn, sid in [
            (iac_agent_fn, "BedrockInvokeIacAgent"),
            (code_generator, "BedrockInvokeGenerator"),
            (validator, "BedrockInvokeValidator"),
            (security_scanner, "BedrockInvokeScanner"),
            (artifact_uploader, "BedrockInvokeUploader"),
        ]:
            fn.add_permission(
                sid,
                principal=iam.ServicePrincipal("bedrock.amazonaws.com"),
                source_arn=cfn_agent.attr_agent_arn,
            )

        # Staging alias — routing_configuration intentionally omitted so that
        # CloudFormation automatically creates a new numbered version from DRAFT
        # on every deploy and routes this alias to it. Integration tests run
        # against this alias. After tests pass, scripts/promote_agent.py reads
        # the version number from this alias and applies it to the production
        # alias — no boto3 create_agent_version call required.
        # description includes the model ID so that any model change causes
        # CloudFormation to update this resource, which triggers creation of a
        # new agent version from DRAFT and re-routes the alias to it.
        staging_alias = bedrock.CfnAgentAlias(
            self,
            "StagingAlias",
            agent_id=cfn_agent.attr_agent_id,
            agent_alias_name="staging",
            description=f"Staging alias — {BEDROCK_MODEL_ID}",
        )

        # The production alias is NOT managed by CDK. It is created/updated by
        # scripts/promote_agent.py only after integration tests pass, ensuring
        # production always points to a tested version.

        # ── Custom resource: clean up unmanaged aliases before agent deletion ─
        # Bedrock refuses to delete an agent while any alias exists (HTTP 409).
        # CFN only knows about StagingAlias; the production alias is invisible to
        # it. This custom resource runs on Delete (before CFN deletes the agent)
        # and removes any aliases CFN does not own, unblocking the teardown.
        alias_cleanup_log_group = logs.LogGroup(
            self,
            "AliasCleanupLogGroup",
            log_group_name=f"/aws/lambda/{AGENT_NAME}-alias-cleanup",
            retention=log_retention,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        alias_cleanup_role = iam.Role(
            self,
            "AliasCleanupRole",
            role_name=f"{AGENT_NAME}-alias-cleanup-role",
            assumed_by=lambda_trust,
        )
        alias_cleanup_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ListAgentAliases", "bedrock:DeleteAgentAlias"],
                resources=[
                    f"arn:aws:bedrock:{region}:{account}:agent/{cfn_agent.attr_agent_id}",
                    f"arn:aws:bedrock:{region}:{account}:agent-alias/{cfn_agent.attr_agent_id}/*",
                ],
            )
        )
        alias_cleanup_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{alias_cleanup_log_group.log_group_arn}:*"],
            )
        )

        _cleanup_code = "\n".join([
            "import boto3",
            "def handler(event, context):",
            "    rid = event.get('PhysicalResourceId', 'alias-cleanup')",
            "    if event['RequestType'] != 'Delete':",
            "        return {'PhysicalResourceId': rid}",
            "    props = event['ResourceProperties']",
            "    agent_id = props['AgentId']",
            "    managed = set(props.get('ManagedAliasIds', []))",
            "    client = boto3.client('bedrock-agent')",
            "    paginator = client.get_paginator('list_agent_aliases')",
            "    for page in paginator.paginate(agentId=agent_id):",
            "        for a in page['agentAliasSummaries']:",
            "            if a['agentAliasId'] not in managed:",
            "                try:",
            "                    client.delete_agent_alias(",
            "                        agentId=agent_id, agentAliasId=a['agentAliasId']",
            "                    )",
            "                except Exception:",
            "                    pass",
            "    return {'PhysicalResourceId': rid}",
        ])

        alias_cleanup_fn = lambda_.Function(
            self,
            "AliasCleanupFn",
            function_name=f"{AGENT_NAME}-alias-cleanup",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(_cleanup_code),
            role=alias_cleanup_role,
            timeout=cdk.Duration.seconds(60),
            log_group=alias_cleanup_log_group,
        )

        alias_cleanup_provider = cr.Provider(
            self,
            "AliasCleanupProvider",
            on_event_handler=alias_cleanup_fn,
        )

        alias_cleanup = cdk.CustomResource(
            self,
            "AliasCleanup",
            service_token=alias_cleanup_provider.service_token,
            properties={
                "AgentId": cfn_agent.attr_agent_id,
                # Tell the handler which alias IDs belong to this stack so it
                # leaves them alone (CFN will delete them in the normal order).
                "ManagedAliasIds": [staging_alias.attr_agent_alias_id],
            },
        )
        # Deletion order: AliasCleanup → StagingAlias → IacAgent
        # (reverse of creation order: IacAgent → StagingAlias → AliasCleanup)
        alias_cleanup.node.add_dependency(staging_alias)

        # SSM parameters — read by OrchestratorStack and the deploy workflow.
        ssm.StringParameter(
            self,
            "AgentIdParam",
            parameter_name=f"/{project_name}/infra-agent/agent-id",
            string_value=cfn_agent.attr_agent_id,
        )
        # production alias-id is written by scripts/promote_agent.py after tests pass.

        cdk.CfnOutput(self, "AgentId", value=cfn_agent.attr_agent_id)
        cdk.CfnOutput(self, "AgentArn", value=cfn_agent.attr_agent_arn)
        cdk.CfnOutput(self, "StagingAliasId", value=staging_alias.attr_agent_alias_id)
