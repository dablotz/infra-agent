import json
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
    aws_ssm as ssm,
)
from constructs import Construct

# Bedrock cross-region inference profile for Claude Sonnet
BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# Strip the cross-region prefix (e.g. "us.") to get the base foundation model
# ID used in regional ARNs.
# e.g. "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
#    → "anthropic.claude-sonnet-4-5-20250929-v1:0"
BEDROCK_BASE_MODEL_ID = re.sub(r"^[a-z]{2}\.", "", BEDROCK_MODEL_ID)

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

        # The production alias is intentionally NOT managed by CDK.
        # CDK deploys only update the DRAFT version. After integration tests
        # pass against TSTALIASID (DRAFT), scripts/promote_agent.py creates a
        # numbered version and updates the production alias. This ensures
        # production is only ever promoted to a tested version.

        # SSM parameters — read by OrchestratorStack and the deploy workflow.
        ssm.StringParameter(
            self,
            "AgentIdParam",
            parameter_name=f"/{project_name}/infra-agent/agent-id",
            string_value=cfn_agent.attr_agent_id,
        )
        # alias-id is written by scripts/promote_agent.py after integration tests pass.

        cdk.CfnOutput(self, "AgentId", value=cfn_agent.attr_agent_id)
        cdk.CfnOutput(self, "AgentArn", value=cfn_agent.attr_agent_arn)
