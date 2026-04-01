"""
Gap resolver for the diagram-to-Terraform pipeline.

Identifies required parameters for each Terraform resource in the IR that have
no value in the manifest.  Applies safe agent defaults where possible, and
returns unresolvable gaps to the orchestrator for user resolution.

The manifest is treated as append-only: existing parameter entries are never
modified or removed — only new entries are appended.
"""

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Resource type → required configuration parameters
#
# Only scalar config values are listed here.  Cross-resource references
# (subnet_id, vpc_id, security_group_ids, etc.) are resolved at HCL
# generation time via Terraform resource references and are intentionally
# excluded from gap detection.
# ---------------------------------------------------------------------------
RESOURCE_REQUIRED_PARAMS: dict[str, list[str]] = {
    "aws_instance": ["ami", "instance_type"],
    "aws_s3_bucket": [],
    "aws_lambda_function": ["runtime", "handler", "role"],
    "aws_rds_instance": [
        "instance_class",
        "engine",
        "engine_version",
        "allocated_storage",
        "username",
        "password",
    ],
    "aws_dynamodb_table": ["hash_key", "billing_mode"],
    "aws_vpc": ["cidr_block"],
    "aws_subnet": ["cidr_block", "availability_zone"],
    "aws_security_group": ["description"],
    "aws_lb": ["internal", "load_balancer_type"],
    "aws_alb": ["internal", "load_balancer_type"],  # legacy alias
    "aws_sns_topic": [],
    "aws_sqs_queue": [],
    "aws_iam_role": ["assume_role_policy"],
    "aws_cloudwatch_metric_alarm": [
        "comparison_operator",
        "evaluation_periods",
        "metric_name",
        "namespace",
        "period",
        "statistic",
        "threshold",
    ],
    "aws_api_gateway_rest_api": [],
    "aws_ecr_repository": [],
    "aws_ecs_cluster": [],
    "aws_ecs_service": ["desired_count"],
    "aws_ecs_task_definition": ["container_definitions"],
    "aws_eks_cluster": ["role_arn"],
}

# ---------------------------------------------------------------------------
# Safe defaults: (value, reasoning)
#
# Every entry here can be applied without additional context.  The reasoning
# string is written verbatim into the manifest and, later, as an inline HCL
# comment by the prompt builder.
# ---------------------------------------------------------------------------
PARAM_DEFAULTS: dict[str, tuple[str, str]] = {
    "instance_type": (
        "t3.micro",
        "Defaulted to t3.micro as a cost-effective general-purpose baseline. "
        "Review and right-size for production workloads based on CPU and memory requirements.",
    ),
    "instance_class": (
        "db.t3.micro",
        "Defaulted to db.t3.micro as the minimum viable RDS instance class. "
        "Review and upgrade for production workloads based on throughput requirements.",
    ),
    "runtime": (
        "python3.12",
        "Defaulted to Python 3.12 (latest stable Lambda runtime). "
        "Update to match the actual function runtime before deployment.",
    ),
    "billing_mode": (
        "PAY_PER_REQUEST",
        "Defaulted to PAY_PER_REQUEST (on-demand) DynamoDB billing to avoid up-front "
        "capacity planning. Switch to PROVISIONED for predictable high-throughput workloads.",
    ),
    "availability_zone": (
        "us-east-1a",
        "Defaulted to us-east-1a. Verify this matches the target region and review "
        "multi-AZ placement requirements before production use.",
    ),
    "internal": (
        "false",
        "Defaulted load balancer to internet-facing (internal=false). "
        "Set to true if this load balancer should only be reachable within the VPC.",
    ),
    "load_balancer_type": (
        "application",
        "Defaulted to Application Load Balancer. Use 'network' for TCP/UDP traffic "
        "or 'gateway' for virtual network appliances.",
    ),
    "desired_count": (
        "1",
        "Defaulted ECS service desired count to 1 for initial deployment. "
        "Increase for production availability and configure auto-scaling as appropriate.",
    ),
    "description": (
        "Managed by Terraform",
        "Defaulted security group description to a placeholder. "
        "Update with a meaningful description of the group's purpose before production use.",
    ),
    "allocated_storage": (
        "20",
        "Defaulted to 20 GB (minimum RDS storage allocation). "
        "Review actual data requirements and enable storage autoscaling before production use.",
    ),
}

# cidr_block defaults are context-sensitive (VPC vs. subnet), so they are
# handled separately rather than in PARAM_DEFAULTS.
CIDR_DEFAULTS: dict[str, tuple[str, str]] = {
    "aws_vpc": (
        "10.0.0.0/16",
        "Defaulted VPC CIDR to 10.0.0.0/16 (a standard private range). "
        "Adjust before production use to avoid overlaps with on-premises or peered VPC ranges.",
    ),
    "aws_subnet": (
        "10.0.1.0/24",
        "Defaulted subnet CIDR to 10.0.1.0/24. "
        "Ensure this falls within the parent VPC CIDR and does not overlap other subnets.",
    ),
}

# engine_version defaults keyed by RDS engine type
ENGINE_VERSION_DEFAULTS: dict[str, tuple[str, str]] = {
    "mysql": (
        "8.0",
        "Defaulted MySQL engine version to 8.0 (current stable major version). "
        "Review for application compatibility before production use.",
    ),
    "postgres": (
        "16.1",
        "Defaulted PostgreSQL engine version to 16.1 (current stable). "
        "Review for application compatibility and consider upgrade paths.",
    ),
    "mariadb": (
        "10.11.5",
        "Defaulted MariaDB engine version to 10.11.5 (LTS). "
        "Review for compatibility before production use.",
    ),
    "aurora-mysql": (
        "8.0.mysql_aurora.3.05.2",
        "Defaulted Aurora MySQL to 8.0-compatible version. "
        "Review Aurora-specific version constraints before production use.",
    ),
    "aurora-postgresql": (
        "16.1",
        "Defaulted Aurora PostgreSQL version to 16.1. "
        "Review for compatibility with your Aurora cluster configuration.",
    ),
    "oracle-ee": (
        "19.0.0.0.ru-2024-04.rur-2024-04.r1",
        "Defaulted Oracle EE to 19c (LTS). "
        "Review Oracle licensing and version requirements for your environment.",
    ),
    "sqlserver-ex": (
        "15.00.4355.3.v1",
        "Defaulted SQL Server Express to 15.00 (SQL Server 2019). "
        "Review licensing constraints and version requirements.",
    ),
    "sqlserver-se": (
        "15.00.4355.3.v1",
        "Defaulted SQL Server SE to 15.00 (SQL Server 2019). "
        "Review licensing constraints and version requirements.",
    ),
    "sqlserver-ee": (
        "15.00.4355.3.v1",
        "Defaulted SQL Server EE to 15.00 (SQL Server 2019). "
        "Review licensing constraints and version requirements.",
    ),
}

# Human-readable reasons why certain params cannot be safely defaulted
GAP_REASONS: dict[str, str] = {
    "ami": (
        "AMI IDs are region- and account-specific. "
        "Provide a valid AMI ID for your target region "
        "(e.g., the latest Amazon Linux 2023 or Ubuntu AMI)."
    ),
    "role": (
        "Lambda execution role ARN is account-specific. "
        "Create or reference an existing IAM role with lambda.amazonaws.com trust "
        "and provide its full ARN."
    ),
    "handler": (
        "Lambda handler is function-specific (e.g., 'index.handler'). "
        "Provide the module.function entry point for your deployment package."
    ),
    "engine": (
        "RDS engine type must be specified explicitly "
        "(e.g., 'mysql', 'postgres', 'mariadb', 'aurora-mysql', 'aurora-postgresql')."
    ),
    "username": (
        "Database master username must be provided. "
        "Choose a name meeting the engine's identifier requirements."
    ),
    "password": (
        "Database master password must be provided securely. "
        "Use AWS Secrets Manager or SSM Parameter Store — never hard-code in HCL."
    ),
    "hash_key": (
        "DynamoDB partition key attribute name is data-model specific "
        "and cannot be safely defaulted."
    ),
    "assume_role_policy": (
        "IAM assume-role policy document is account- and service-specific. "
        "Provide a valid JSON policy granting the required service principal trust."
    ),
    "container_definitions": (
        "ECS container definitions are application-specific. "
        "Provide the full definition JSON including image, port mappings, and environment."
    ),
    "role_arn": (
        "EKS cluster IAM role ARN is account-specific. "
        "Provide the ARN of an IAM role with AmazonEKSClusterPolicy attached "
        "and eks.amazonaws.com as the trust principal."
    ),
    "comparison_operator": (
        "CloudWatch alarm comparison operator must be specified "
        "(e.g., 'GreaterThanThreshold', 'LessThanThreshold')."
    ),
    "evaluation_periods": (
        "CloudWatch alarm evaluation period count is SLA-specific. "
        "Provide the number of consecutive breaching periods before the alarm fires."
    ),
    "metric_name": (
        "CloudWatch metric name is service- and use-case-specific "
        "(e.g., 'CPUUtilization', 'Errors', 'Duration')."
    ),
    "namespace": (
        "CloudWatch metric namespace is service-specific "
        "(e.g., 'AWS/EC2', 'AWS/Lambda', 'AWS/RDS')."
    ),
    "period": (
        "CloudWatch alarm evaluation period in seconds is monitoring-requirement-specific "
        "(e.g., 60 for 1 minute, 300 for 5 minutes)."
    ),
    "statistic": (
        "CloudWatch alarm statistic must be specified "
        "(e.g., 'Average', 'Sum', 'Maximum', 'Minimum', 'SampleCount')."
    ),
    "threshold": (
        "CloudWatch alarm threshold value is metric- and business-requirement-specific."
    ),
    "engine_version": (
        "Cannot determine a safe engine version default because the RDS engine type "
        "is unknown. Specify the engine first, then provide a compatible version string."
    ),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _service_slug(service: dict) -> str:
    """Return the manifest-compatible slug for a service (matches diagram parser convention)."""
    label = service.get("label", "")
    if label:
        return label.replace(" ", "_").replace("-", "_").lower()
    return service["id"].replace("-", "_").lower()


def _manifest_lookup(parameters: list[dict], param_key: str) -> str | None:
    """Return the value for param_key in parameters, or None if absent."""
    for p in parameters:
        if p["parameter"] == param_key:
            return p["value"]
    return None


def _resolve_engine_version(
    service: dict,
    all_params: list[dict],
    resource_type: str,
    service_slug: str,
) -> tuple[str | None, str | None]:
    """Return (value, reasoning) for engine_version, using the known engine param."""
    engine_key = f"{resource_type}.{service_slug}.engine"
    engine = _manifest_lookup(all_params, engine_key) or service.get("config", {}).get("engine")
    if not engine:
        return None, None
    return ENGINE_VERSION_DEFAULTS.get(engine.lower(), (None, None))


def _gap_entry(service: dict, resource_type: str, param_key: str, reason: str) -> dict:
    return {
        "service_id": service["id"],
        "resource_type": resource_type,
        "parameter": param_key,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_gaps(ir: dict, manifest: dict) -> tuple[dict, list[dict]]:
    """Identify and resolve parameter gaps for all services in the IR.

    For each service, checks RESOURCE_REQUIRED_PARAMS against the current
    manifest entries. Missing parameters are handled in priority order:

      1. IR config values not yet in the manifest   → appended as source="parsed"
      2. Parameters with safe well-known defaults   → appended as source="agent_default"
      3. Parameters that cannot be safely defaulted → added to the returned gaps list

    The manifest is treated as append-only: existing entries are never mutated.

    Args:
        ir:       Populated IR dict conforming to ir_schema.json
        manifest: Partial or complete manifest dict conforming to manifest_schema.json

    Returns:
        (enriched_manifest, unresolvable_gaps) where enriched_manifest has new
        parameter entries appended and unresolvable_gaps is a (possibly empty)
        list of gap dicts with keys: service_id, resource_type, parameter, reason.
    """
    existing_params: list[dict] = manifest["parameters"]
    new_params: list[dict] = []
    gaps: list[dict] = []

    for service in ir.get("services", []):
        resource_type = service.get("type", "unknown")
        if resource_type == "unknown":
            logger.info(json.dumps({
                "message": "skipping_unknown_service",
                "service_id": service.get("id"),
                "label": service.get("label"),
            }))
            continue

        service_slug = _service_slug(service)
        required_params = RESOURCE_REQUIRED_PARAMS.get(resource_type, [])

        for param in required_params:
            param_key = f"{resource_type}.{service_slug}.{param}"

            # Already resolved in the existing manifest — nothing to do.
            if _manifest_lookup(existing_params, param_key) is not None:
                continue

            # Already resolved earlier in this pass — nothing to do.
            if any(p["parameter"] == param_key for p in new_params):
                continue

            # IR config may contain values that the diagram parser extracted but
            # did not include in the manifest (defensive check).
            ir_config_value = service.get("config", {}).get(param)
            if ir_config_value is not None:
                new_params.append({
                    "parameter": param_key,
                    "value": str(ir_config_value),
                    "source": "parsed",
                    "reasoning": None,
                })
                continue

            # cidr_block: default differs between VPC and subnet.
            if param == "cidr_block":
                if resource_type in CIDR_DEFAULTS:
                    value, reasoning = CIDR_DEFAULTS[resource_type]
                    new_params.append({
                        "parameter": param_key,
                        "value": value,
                        "source": "agent_default",
                        "reasoning": reasoning,
                    })
                else:
                    gaps.append(_gap_entry(
                        service, resource_type, param_key,
                        "CIDR block cannot be safely defaulted for this resource type.",
                    ))
                continue

            # engine_version: default depends on the engine type.
            if param == "engine_version":
                ev_value, ev_reasoning = _resolve_engine_version(
                    service, existing_params + new_params, resource_type, service_slug
                )
                if ev_value is not None:
                    new_params.append({
                        "parameter": param_key,
                        "value": ev_value,
                        "source": "agent_default",
                        "reasoning": ev_reasoning,
                    })
                else:
                    gaps.append(_gap_entry(
                        service, resource_type, param_key,
                        GAP_REASONS.get("engine_version", "Cannot determine engine version."),
                    ))
                continue

            # Standard scalar defaults.
            if param in PARAM_DEFAULTS:
                value, reasoning = PARAM_DEFAULTS[param]
                new_params.append({
                    "parameter": param_key,
                    "value": value,
                    "source": "agent_default",
                    "reasoning": reasoning,
                })
                continue

            # No safe default available — must be resolved by the user.
            reason = GAP_REASONS.get(
                param,
                f"'{param}' has no safe default and must be provided explicitly.",
            )
            gaps.append(_gap_entry(service, resource_type, param_key, reason))

    enriched_manifest = {
        **manifest,
        "parameters": existing_params + new_params,
    }

    logger.info(json.dumps({
        "message": "gap_resolution_complete",
        "defaults_applied": len(new_params),
        "unresolvable_gaps": len(gaps),
    }))

    return enriched_manifest, gaps


def load_from_s3(bucket: str, key: str, s3_client) -> dict:
    """Fetch and parse a JSON object from S3."""
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))
