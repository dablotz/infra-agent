"""
Diagram Parser Lambda — converts draw.io / Lucidchart XML diagrams to a
Normalized Intermediate Representation (IR) and an initial Configuration
Manifest, both written back to S3 alongside the source file.

Trigger:  S3 event (s3:ObjectCreated:*) on *.drawio or *.xml objects.
Outputs:  diagrams/{stem}/ir.json
          diagrams/{stem}/manifest.json
"""

import json
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Shape → Terraform resource type mapping
#
# Keys are normalised shape identifiers (lowercase, "mxgraph." prefix stripped).
# draw.io shapes arrive as  resIcon=mxgraph.aws4.{key}  or  shape=mxgraph.aws4.{key}.
# Lucidchart native types arrive as the element's "type" attribute, e.g. "aws.EC2".
#
# See README.md for step-by-step instructions on adding new shape mappings.
# ---------------------------------------------------------------------------
SHAPE_TO_TERRAFORM: dict[str, str] = {
    # --- Compute ---
    "aws4.ec2": "aws_instance",
    "aws4.ec2_instance": "aws_instance",
    "aws4.lambda_function": "aws_lambda_function",
    "aws4.lambda": "aws_lambda_function",
    "aws4.ecs": "aws_ecs_cluster",
    "aws4.ecs_service": "aws_ecs_service",
    "aws4.eks": "aws_eks_cluster",
    "aws4.auto_scaling": "aws_autoscaling_group",
    "aws4.batch": "aws_batch_job_definition",
    # --- Storage ---
    "aws4.s3": "aws_s3_bucket",
    "aws4.dynamodb": "aws_dynamodb_table",
    "aws4.rds": "aws_db_instance",
    "aws4.rds_instance": "aws_db_instance",
    "aws4.aurora": "aws_rds_cluster",
    "aws4.elasticache": "aws_elasticache_cluster",
    "aws4.efs": "aws_efs_file_system",
    # --- Networking ---
    "aws4.vpc": "aws_vpc",
    "aws4.subnet": "aws_subnet",
    "aws4.security_group": "aws_security_group",
    "aws4.internet_gateway": "aws_internet_gateway",
    "aws4.nat_gateway": "aws_nat_gateway",
    "aws4.route_table": "aws_route_table",
    "aws4.elb": "aws_lb",
    "aws4.application_load_balancer": "aws_lb",
    "aws4.network_load_balancer": "aws_lb",
    "aws4.cloudfront": "aws_cloudfront_distribution",
    "aws4.api_gateway": "aws_api_gateway_rest_api",
    "aws4.api_gateway_v2": "aws_apigatewayv2_api",
    "aws4.route53": "aws_route53_zone",
    "aws4.vpc_endpoint": "aws_vpc_endpoint",
    # --- Messaging ---
    "aws4.sns": "aws_sns_topic",
    "aws4.sqs": "aws_sqs_queue",
    "aws4.kinesis": "aws_kinesis_stream",
    "aws4.kinesis_firehose": "aws_kinesis_firehose_delivery_stream",
    "aws4.eventbridge": "aws_cloudwatch_event_rule",
    "aws4.mq": "aws_mq_broker",
    # --- Security / IAM ---
    "aws4.role": "aws_iam_role",
    "aws4.iam": "aws_iam_role",
    "aws4.kms": "aws_kms_key",
    "aws4.secrets_manager": "aws_secretsmanager_secret",
    "aws4.waf": "aws_waf_web_acl",
    "aws4.cognito": "aws_cognito_user_pool",
    # --- Monitoring ---
    "aws4.cloudwatch": "aws_cloudwatch_metric_alarm",
    "aws4.cloudtrail": "aws_cloudtrail",
    # --- Lucidchart native type IDs (lowercase "aws." prefix) ---
    "aws.ec2": "aws_instance",
    "aws.s3": "aws_s3_bucket",
    "aws.lambda": "aws_lambda_function",
    "aws.rds": "aws_db_instance",
    "aws.vpc": "aws_vpc",
    "aws.subnet": "aws_subnet",
    "aws.security_group": "aws_security_group",
    "aws.elb": "aws_lb",
    "aws.dynamodb": "aws_dynamodb_table",
    "aws.sns": "aws_sns_topic",
    "aws.sqs": "aws_sqs_queue",
    "aws.api_gateway": "aws_api_gateway_rest_api",
    "aws.cloudfront": "aws_cloudfront_distribution",
    "aws.eks": "aws_eks_cluster",
    "aws.ecs": "aws_ecs_cluster",
    "aws.kms": "aws_kms_key",
    "aws.cloudwatch": "aws_cloudwatch_metric_alarm",
}

# Edge label text → IR relationship_type (case-insensitive, stripped)
_EDGE_LABEL_TO_RELATIONSHIP: dict[str, str] = {
    "depends on": "depends_on",
    "depends_on": "depends_on",
    "routes to": "routes_to",
    "routes_to": "routes_to",
    "contained by": "contained_by",
    "contained_by": "contained_by",
    "contains": "contained_by",
    "references": "references",
}

_DEFAULT_RELATIONSHIP = "connects_to"


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def _detect_format(xml_content: str) -> str:
    """Return 'drawio' when the root element is <mxGraphModel>, else 'lucidchart'."""
    root = ET.fromstring(xml_content)
    return "drawio" if root.tag == "mxGraphModel" else "lucidchart"


# ---------------------------------------------------------------------------
# draw.io parser
# ---------------------------------------------------------------------------


def _extract_drawio_shape_key(style: str) -> str | None:
    """
    Parse a draw.io style string and return the normalised shape key.

    Prefers resIcon over shape for resource-icon cells (it is more specific).
    Strips the 'mxgraph.' prefix so the key matches SHAPE_TO_TERRAFORM.
    Returns None when no AWS shape token is found.
    """
    parts: dict[str, str] = {}
    for segment in style.split(";"):
        if "=" in segment:
            k, _, v = segment.partition("=")
            parts[k.strip()] = v.strip()

    raw: str | None = None
    if "resIcon" in parts:
        raw = parts["resIcon"]  # e.g. "mxgraph.aws4.ec2"
    elif "shape" in parts and "aws" in parts["shape"].lower():
        raw = parts["shape"]    # e.g. "mxgraph.aws4.s3"

    if raw is None:
        return None

    return raw.replace("mxgraph.", "").lower()


def _parse_drawio(xml_content: str) -> tuple[list[dict], list[dict]]:
    """
    Parse draw.io native XML and return (services, relationships).

    Vertices whose style contains 'aws' are treated as AWS service nodes.
    All mxCells with edge="1" and both source/target set become relationships.
    """
    root = ET.fromstring(xml_content)
    services: list[dict] = []
    relationships: list[dict] = []

    for cell in root.iter("mxCell"):
        cell_id = cell.get("id", "")
        value = (cell.get("value") or "").strip()
        style = cell.get("style") or ""

        if cell.get("vertex") == "1" and "aws" in style.lower():
            shape_key = _extract_drawio_shape_key(style)
            tf_type = SHAPE_TO_TERRAFORM.get(shape_key, "unknown") if shape_key else "unknown"
            services.append({
                "id": cell_id,
                "type": tf_type,
                "label": value or cell_id,
                "config": {},
            })

        elif cell.get("edge") == "1":
            source = cell.get("source")
            target = cell.get("target")
            if source and target:
                label_norm = value.lower()
                rel_type = _EDGE_LABEL_TO_RELATIONSHIP.get(label_norm, _DEFAULT_RELATIONSHIP)
                relationships.append({
                    "source": source,
                    "target": target,
                    "relationship_type": rel_type,
                    "label": value or None,
                })

    return services, relationships


# ---------------------------------------------------------------------------
# Lucidchart parser
# ---------------------------------------------------------------------------


def _parse_lucidchart(xml_content: str) -> tuple[list[dict], list[dict]]:
    """
    Parse Lucidchart native export XML and return (services, relationships).

    Expected document structure::

        <drawing>
          <page id="...">
            <elements>
              <element id="..." type="aws.EC2">
                <text>Label</text>
              </element>
            </elements>
            <connections>
              <connection id="..." from="..." to="..." label="depends on"/>
            </connections>
          </page>
        </drawing>

    Elements whose type does not start with "aws" are silently skipped.
    """
    root = ET.fromstring(xml_content)
    services: list[dict] = []
    relationships: list[dict] = []

    for elem in root.iter("element"):
        elem_id = elem.get("id", "")
        type_raw = (elem.get("type") or "").lower()  # e.g. "aws.EC2" → "aws.ec2"
        if not type_raw.startswith("aws"):
            continue

        text_el = elem.find("text")
        label = (text_el.text or "").strip() if text_el is not None else ""
        tf_type = SHAPE_TO_TERRAFORM.get(type_raw, "unknown")
        services.append({
            "id": elem_id,
            "type": tf_type,
            "label": label or elem_id,
            "config": {},
        })

    for conn in root.iter("connection"):
        source = conn.get("from")
        target = conn.get("to")
        if source and target:
            label_norm = (conn.get("label") or "").strip().lower()
            rel_type = _EDGE_LABEL_TO_RELATIONSHIP.get(label_norm, _DEFAULT_RELATIONSHIP)
            relationships.append({
                "source": source,
                "target": target,
                "relationship_type": rel_type,
                "label": conn.get("label") or None,
            })

    return services, relationships


# ---------------------------------------------------------------------------
# IR and Manifest builders
# ---------------------------------------------------------------------------


def _extract_network(services: list[dict]) -> dict:
    """Derive the network section from the services list."""
    network: dict = {"vpcs": [], "subnets": [], "security_groups": []}
    for svc in services:
        if svc["type"] == "aws_vpc":
            network["vpcs"].append({
                "id": svc["id"],
                "label": svc["label"],
                "cidr_block": svc["config"].get("cidr_block"),
            })
        elif svc["type"] == "aws_subnet":
            network["subnets"].append({
                "id": svc["id"],
                "label": svc["label"],
                "vpc_id": svc["config"].get("vpc_id"),
                "availability_zone": svc["config"].get("availability_zone"),
            })
        elif svc["type"] == "aws_security_group":
            network["security_groups"].append({
                "id": svc["id"],
                "label": svc["label"],
                "description": svc["config"].get("description"),
            })
    return network


def _slugify(label: str) -> str:
    """Convert a human label to a Terraform-safe identifier."""
    return label.lower().replace(" ", "_").replace("-", "_").replace(".", "_")


def _build_ir(services: list[dict], relationships: list[dict], source_file: str) -> dict:
    return {
        "schema_version": "1.0",
        "source_file": source_file,
        "services": services,
        "relationships": relationships,
        "network": _extract_network(services),
    }


def _build_manifest(services: list[dict], ir_source: str) -> dict:
    """
    Build an initial manifest where every extracted value carries source='parsed'.
    Downstream agents are responsible for adding agent_default entries for
    parameters not present in the diagram (e.g. instance_type, ami_id).
    """
    parameters: list[dict] = []
    for svc in services:
        resource_addr = f"{svc['type']}.{_slugify(svc['label'])}"
        # Diagram node id allows agents to cross-reference back to the IR
        parameters.append({
            "parameter": f"{resource_addr}.diagram_id",
            "value": svc["id"],
            "source": "parsed",
            "reasoning": None,
        })
        for k, v in svc["config"].items():
            parameters.append({
                "parameter": f"{resource_addr}.{k}",
                "value": v,
                "source": "parsed",
                "reasoning": None,
            })
    return {
        "schema_version": "1.0",
        "ir_source": ir_source,
        "parameters": parameters,
    }


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def lambda_handler(event, context, s3_client=None):
    """
    S3-triggered Lambda handler.

    Downloads a .drawio/.xml diagram from S3, parses it into the Normalized IR
    and Configuration Manifest schemas, then writes both JSON files back to S3.

    Args:
        event:     AWS S3 event dict (Records[0].s3.bucket / object).
        context:   AWS Lambda context object (unused directly).
        s3_client: Optional pre-built boto3 S3 client — injected by unit tests
                   to avoid real AWS calls.

    Returns:
        dict containing ir_s3_key, manifest_s3_key, and service_count.
    """
    if s3_client is None:
        s3_client = boto3.client("s3")

    record = event["Records"][0]
    bucket = record["s3"]["bucket"]["name"]
    key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

    logger.info("Processing diagram: s3://%s/%s", bucket, key)

    obj = s3_client.get_object(Bucket=bucket, Key=key)
    xml_content = obj["Body"].read().decode("utf-8")

    fmt = _detect_format(xml_content)
    logger.info("Detected format: %s", fmt)

    if fmt == "drawio":
        services, relationships = _parse_drawio(xml_content)
    else:
        services, relationships = _parse_lucidchart(xml_content)

    logger.info("Extracted %d services, %d relationships", len(services), len(relationships))

    stem = PurePosixPath(key).stem   # "diagrams/my-arch.drawio" → "my-arch"
    ir_key = f"diagrams/{stem}/ir.json"
    manifest_key = f"diagrams/{stem}/manifest.json"

    ir = _build_ir(services, relationships, source_file=key)
    manifest = _build_manifest(services, ir_source=key)

    s3_client.put_object(
        Bucket=bucket,
        Key=ir_key,
        Body=json.dumps(ir, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    s3_client.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    logger.info("Wrote IR → s3://%s/%s", bucket, ir_key)
    logger.info("Wrote manifest → s3://%s/%s", bucket, manifest_key)

    return {
        "ir_s3_key": ir_key,
        "manifest_s3_key": manifest_key,
        "service_count": len(services),
    }
