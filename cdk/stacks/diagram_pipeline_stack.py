import os

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
    aws_ssm as ssm,
)
from constructs import Construct

from cdk.constants import BEDROCK_MODEL_ID


class DiagramPipelineStack(Stack):
    """Infrastructure for the diagram-to-IaC preprocessing pipeline.

    Creates the diagrams S3 bucket, parser Lambdas, upload router, SSM
    parameters, and the S3 → upload_router event notification.

    The iac_agent Lambda and its ProcessDiagram action group live in
    InfraAgentStack (alongside the Bedrock agent they extend). This stack
    grants iac_agent_role read/write access to the diagrams bucket using the
    role reference exported from InfraAgentStack.

    Deploy order:
        1. InfraAgentStack   (exports iac_agent_role)
        2. OrchestratorStack (exports orchestrator agent ID / alias)
        3. DiagramPipelineStack

    Context values (pass via -c flags or cdk.json):
        orchestrator_agent_id    Bedrock orchestrator agent ID
        orchestrator_alias_id    Bedrock orchestrator agent alias ID
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        project_name: str,
        orchestrator_agent_id: str,
        orchestrator_alias_id: str,
        iac_agent_role: iam.IRole,
        log_retention: logs.RetentionDays = logs.RetentionDays.ONE_MONTH,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account = self.account
        region = self.region

        here = os.path.dirname(__file__)
        repo_root = os.path.abspath(os.path.join(here, "..", ".."))
        infra_lambda_dir = os.path.join(
            repo_root, "agents", "infra-agent", "lambda_functions"
        )
        orchestration_dir = os.path.join(repo_root, "orchestration")

        # ── SSM parameters ───────────────────────────────────────────────────
        ssm.StringParameter(
            self,
            "RekognitionConfidenceThreshold",
            parameter_name=f"/{project_name}/diagram-pipeline/rekognition-confidence-threshold",
            string_value="70",
            description="Min Rekognition label confidence (0–100) to include a detected service in the IR",
        )
        ssm.StringParameter(
            self,
            "DefaultRegion",
            parameter_name=f"/{project_name}/diagram-pipeline/default-region",
            string_value=region,
            description="AWS region assumed when generating region-specific resource defaults",
        )
        ssm.StringParameter(
            self,
            "BedrockModelId",
            parameter_name=f"/{project_name}/diagram-pipeline/bedrock-model-id",
            string_value=BEDROCK_MODEL_ID,
            description="Bedrock model ID used by the PNG vision pipeline",
        )

        # ── S3: diagrams bucket ──────────────────────────────────────────────
        diagrams_bucket = s3.Bucket(
            self,
            "DiagramsBucket",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-raw-uploads",
                    enabled=True,
                    expiration=cdk.Duration.days(30),
                    noncurrent_version_expiration=cdk.Duration.days(7),
                )
            ],
        )

        # Grant iac_agent (defined in InfraAgentStack) read/write on this bucket
        diagrams_bucket.grant_read_write(iac_agent_role)

        # ── CloudWatch log groups ────────────────────────────────────────────
        log_group_parser = logs.LogGroup(
            self,
            "LogGroupDiagramParser",
            log_group_name=f"/aws/lambda/{project_name}-diagram-parser",
            retention=log_retention,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        log_group_png = logs.LogGroup(
            self,
            "LogGroupPngPipeline",
            log_group_name=f"/aws/lambda/{project_name}-png-pipeline",
            retention=log_retention,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        log_group_router = logs.LogGroup(
            self,
            "LogGroupUploadRouter",
            log_group_name=f"/aws/lambda/{project_name}-upload-router",
            retention=log_retention,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        lambda_trust = iam.ServicePrincipal("lambda.amazonaws.com")

        # ── IAM + Lambda: diagram_parser ─────────────────────────────────────
        parser_role = iam.Role(
            self,
            "DiagramParserRole",
            role_name=f"{project_name}-diagram-parser-role",
            assumed_by=lambda_trust,
        )
        diagrams_bucket.grant_read(parser_role)
        diagrams_bucket.grant_put(parser_role, "diagrams/*")
        parser_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group_parser.log_group_arn}:*"],
            )
        )

        diagram_parser = lambda_.Function(
            self,
            "DiagramParser",
            function_name=f"{project_name}-diagram-parser",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(infra_lambda_dir, "diagram_parser"),
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output --quiet"
                        " && cp -r . /asset-output",
                    ],
                ),
            ),
            role=parser_role,
            timeout=cdk.Duration.seconds(60),
            memory_size=256,
            environment={"OUTPUT_BUCKET": diagrams_bucket.bucket_name},
            log_group=log_group_parser,
        )

        # ── IAM + Lambda: png_pipeline ───────────────────────────────────────
        png_role = iam.Role(
            self,
            "PngPipelineRole",
            role_name=f"{project_name}-png-pipeline-role",
            assumed_by=lambda_trust,
        )
        diagrams_bucket.grant_read(png_role)
        diagrams_bucket.grant_put(png_role, "diagrams/*")
        png_role.add_to_policy(
            iam.PolicyStatement(
                actions=["rekognition:DetectLabels"],
                resources=["*"],
            )
        )
        png_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/{BEDROCK_MODEL_ID}",
                    f"arn:aws:bedrock:us-east-1::foundation-model/*",
                    f"arn:aws:bedrock:us-west-2::foundation-model/*",
                    f"arn:aws:bedrock:us-east-2::foundation-model/*",
                ],
            )
        )
        png_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group_png.log_group_arn}:*"],
            )
        )

        png_pipeline = lambda_.Function(
            self,
            "PngPipeline",
            function_name=f"{project_name}-png-pipeline",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                os.path.join(infra_lambda_dir, "diagram_parser", "png_pipeline")
            ),
            role=png_role,
            timeout=cdk.Duration.seconds(120),
            memory_size=512,
            environment={
                "OUTPUT_BUCKET": diagrams_bucket.bucket_name,
                "BEDROCK_MODEL_ID": BEDROCK_MODEL_ID,
            },
            log_group=log_group_png,
        )

        # ── IAM + Lambda: upload_router ──────────────────────────────────────
        router_role = iam.Role(
            self,
            "UploadRouterRole",
            role_name=f"{project_name}-upload-router-role",
            assumed_by=lambda_trust,
        )
        # HeadObject to read x-amz-meta-user-request from uploaded files
        diagrams_bucket.grant_read(router_role)
        router_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    diagram_parser.function_arn,
                    png_pipeline.function_arn,
                ],
            )
        )
        router_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeAgent"],
                resources=[
                    f"arn:aws:bedrock:{region}:{account}:agent/{orchestrator_agent_id}",
                    f"arn:aws:bedrock:{region}:{account}:agent-alias/{orchestrator_agent_id}/{orchestrator_alias_id}",
                ],
            )
        )
        router_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group_router.log_group_arn}:*"],
            )
        )

        upload_router = lambda_.Function(
            self,
            "UploadRouter",
            function_name=f"{project_name}-upload-router",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="upload_router.lambda_handler",
            code=lambda_.Code.from_asset(orchestration_dir),
            role=router_role,
            timeout=cdk.Duration.seconds(180),
            memory_size=256,
            environment={
                "DIAGRAM_PARSER_FUNCTION": diagram_parser.function_name,
                "PNG_PIPELINE_FUNCTION": png_pipeline.function_name,
                "ORCHESTRATOR_AGENT_ID": orchestrator_agent_id,
                "ORCHESTRATOR_AGENT_ALIAS_ID": orchestrator_alias_id,
            },
            log_group=log_group_router,
        )

        # ── S3 → upload_router event notification ────────────────────────────
        # Fires on every ObjectCreated event; unsupported extensions are
        # filtered in upload_router.lambda_handler, not at the bucket level.
        diagrams_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(upload_router),
        )

        # ── Outputs ──────────────────────────────────────────────────────────
        cdk.CfnOutput(
            self,
            "DiagramsBucketName",
            value=diagrams_bucket.bucket_name,
            description="Upload architecture diagrams here to trigger the pipeline",
        )
        cdk.CfnOutput(
            self,
            "UploadRouterFunctionName",
            value=upload_router.function_name,
        )
