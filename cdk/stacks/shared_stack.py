import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_s3 as s3,
    aws_events as events,
    aws_iam as iam,
    aws_ssm as ssm,
)
from constructs import Construct


class SharedStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        project_name: str,
        github_repo: str,
        create_oidc_provider: bool,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account = self.account

        # S3 bucket for agent artifacts (runbooks, outputs, etc.)
        artifact_bucket = s3.Bucket(
            self,
            "AgentArtifacts",
            bucket_name=f"{project_name}-agent-artifacts-{account}",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # S3 bucket for Lambda layers (shared across all agent stacks)
        layers_bucket = s3.Bucket(
            self,
            "LambdaLayers",
            bucket_name=f"{project_name}-lambda-layers-{account}",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # EventBridge custom bus for inter-agent communication
        event_bus = events.EventBus(
            self,
            "AgentBus",
            event_bus_name=f"{project_name}-agent-bus",
        )

        # GitHub Actions OIDC — lets CI assume an IAM role using a short-lived
        # token issued by GitHub, with no long-lived AWS credentials stored as secrets.
        # Enable by setting github_repo = "owner/repo-name".
        # If the OIDC provider already exists in this account, set
        # create_github_oidc_provider = false to skip creating a duplicate.
        if github_repo:
            if create_oidc_provider:
                oidc_provider = iam.OpenIdConnectProvider(
                    self,
                    "GitHubOidc",
                    url="https://token.actions.githubusercontent.com",
                    client_ids=["sts.amazonaws.com"],
                    thumbprints=[
                        "6938fd4d98bab03faadb97b34396831e3780aea1",
                        "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
                    ],
                )
                oidc_provider_arn = oidc_provider.open_id_connect_provider_arn
            else:
                oidc_provider_arn = f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com"

            # AdministratorAccess is used here because CDK needs to create and
            # manage IAM roles, policies, and a broad set of services. The blast
            # radius is bounded by the OIDC trust policy, which restricts access
            # to a single repository on the main branch only.
            ci_role = iam.Role(
                self,
                "CiDeployRole",
                role_name=f"{project_name}-ci-deploy",
                assumed_by=iam.FederatedPrincipal(
                    oidc_provider_arn,
                    conditions={
                        "StringEquals": {
                            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                        },
                        # Scoped to pushes on main only — PRs use read-only
                        # token and don't deploy.
                        "StringLike": {
                            "token.actions.githubusercontent.com:sub": f"repo:{github_repo}:ref:refs/heads/main"
                        },
                    },
                    assume_role_action="sts:AssumeRoleWithWebIdentity",
                ),
            )
            ci_role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess")
            )

            ssm.StringParameter(
                self,
                "CiRoleArnParam",
                parameter_name=f"/{project_name}/ci-deploy-role-arn",
                string_value=ci_role.role_arn,
            )

        # SSM parameters — consumed by other agent stacks and CI tooling
        ssm.StringParameter(
            self,
            "ArtifactBucketParam",
            parameter_name=f"/{project_name}/artifact-bucket",
            string_value=artifact_bucket.bucket_name,
        )
        ssm.StringParameter(
            self,
            "LayersBucketParam",
            parameter_name=f"/{project_name}/layers-bucket",
            string_value=layers_bucket.bucket_name,
        )
        ssm.StringParameter(
            self,
            "EventBusNameParam",
            parameter_name=f"/{project_name}/event-bus-name",
            string_value=event_bus.event_bus_name,
        )
        ssm.StringParameter(
            self,
            "EventBusArnParam",
            parameter_name=f"/{project_name}/event-bus-arn",
            string_value=event_bus.event_bus_arn,
        )

        cdk.CfnOutput(self, "ArtifactBucket", value=artifact_bucket.bucket_name)
        cdk.CfnOutput(self, "LayersBucket", value=layers_bucket.bucket_name)
        cdk.CfnOutput(self, "EventBusArn", value=event_bus.event_bus_arn)
        if github_repo:
            cdk.CfnOutput(
                self,
                "CiDeployRoleArn",
                value=ci_role.role_arn,
                description="Set this as the AWS_DEPLOY_ROLE_ARN secret in GitHub",
            )
