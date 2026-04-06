"""
Unit tests for the diagram_parser Lambda handler.

Covers both draw.io native XML and Lucidchart XML formats.
All tests run locally with no AWS account — S3 calls are mocked.
"""

import importlib.util
import io
import json
import pathlib
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Load the handler module dynamically (avoids sys.modules collisions)
# ---------------------------------------------------------------------------
_HANDLER_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "lambda_functions"
    / "diagram_parser"
    / "handler.py"
)
_spec = importlib.util.spec_from_file_location("diagram_parser", _HANDLER_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

lambda_handler = _mod.lambda_handler
SHAPE_TO_TERRAFORM = _mod.SHAPE_TO_TERRAFORM

# Re-export internal helpers for white-box tests
_detect_format = _mod._detect_format
_extract_drawio_shape_key = _mod._extract_drawio_shape_key
_parse_drawio = _mod._parse_drawio
_parse_lucidchart = _mod._parse_lucidchart
_extract_network = _mod._extract_network
_build_manifest = _mod._build_manifest
_slugify = _mod.slugify


# ---------------------------------------------------------------------------
# Fixtures — shared XML documents
# ---------------------------------------------------------------------------

DRAWIO_XML_BASIC = """
<mxGraphModel>
  <root>
    <mxCell id="0"/>
    <mxCell id="1" parent="0"/>
    <mxCell id="ec2-1" value="Web Server"
            style="shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.ec2;labelBackgroundColor=#ffffff;"
            vertex="1" parent="1">
      <mxGeometry x="100" y="100" width="78" height="78" as="geometry"/>
    </mxCell>
    <mxCell id="s3-1" value="Storage Bucket"
            style="shape=mxgraph.aws4.s3;"
            vertex="1" parent="1">
      <mxGeometry x="300" y="100" width="78" height="78" as="geometry"/>
    </mxCell>
    <mxCell id="rect-1" value="A plain box"
            style="rounded=1;whiteSpace=wrap;"
            vertex="1" parent="1">
      <mxGeometry x="0" y="0" width="100" height="50" as="geometry"/>
    </mxCell>
    <mxCell id="edge-1" value="" edge="1" source="ec2-1" target="s3-1" parent="1">
      <mxGeometry relative="1" as="geometry"/>
    </mxCell>
  </root>
</mxGraphModel>
"""

DRAWIO_XML_LABELED_EDGE = """
<mxGraphModel>
  <root>
    <mxCell id="0"/>
    <mxCell id="1" parent="0"/>
    <mxCell id="lambda-1" value="Processor"
            style="shape=mxgraph.aws4.lambda;"
            vertex="1" parent="1"/>
    <mxCell id="db-1" value="Orders DB"
            style="shape=mxgraph.aws4.rds;"
            vertex="1" parent="1"/>
    <mxCell id="edge-1" value="depends on" edge="1" source="lambda-1" target="db-1" parent="1">
      <mxGeometry relative="1" as="geometry"/>
    </mxCell>
  </root>
</mxGraphModel>
"""

DRAWIO_XML_NETWORK = """
<mxGraphModel>
  <root>
    <mxCell id="0"/>
    <mxCell id="1" parent="0"/>
    <mxCell id="vpc-1" value="Main VPC"
            style="shape=mxgraph.aws4.vpc;"
            vertex="1" parent="1"/>
    <mxCell id="subnet-1" value="Public Subnet"
            style="shape=mxgraph.aws4.subnet;"
            vertex="1" parent="1"/>
    <mxCell id="sg-1" value="Web SG"
            style="shape=mxgraph.aws4.security_group;"
            vertex="1" parent="1"/>
  </root>
</mxGraphModel>
"""

DRAWIO_XML_UNKNOWN_SHAPE = """
<mxGraphModel>
  <root>
    <mxCell id="0"/>
    <mxCell id="1" parent="0"/>
    <mxCell id="x-1" value="Future Service"
            style="shape=mxgraph.aws4.new_service_not_in_map;"
            vertex="1" parent="1"/>
  </root>
</mxGraphModel>
"""

LUCIDCHART_XML_BASIC = """
<drawing>
  <page id="page-1">
    <elements>
      <element id="elem-1" type="aws.EC2">
        <text>App Server</text>
      </element>
      <element id="elem-2" type="aws.RDS">
        <text>Database</text>
      </element>
      <element id="elem-3" type="generic">
        <text>Not an AWS service</text>
      </element>
    </elements>
    <connections>
      <connection id="conn-1" from="elem-1" to="elem-2" label="depends on"/>
    </connections>
  </page>
</drawing>
"""

LUCIDCHART_XML_UNLABELED_EDGE = """
<drawing>
  <page id="page-1">
    <elements>
      <element id="n-1" type="aws.Lambda">
        <text>Processor</text>
      </element>
      <element id="n-2" type="aws.SQS">
        <text>Queue</text>
      </element>
    </elements>
    <connections>
      <connection id="c-1" from="n-1" to="n-2"/>
    </connections>
  </page>
</drawing>
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _s3_event(bucket: str, key: str) -> dict:
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}


def _mock_s3(xml_content: str) -> MagicMock:
    """Return a mock S3 client that serves xml_content on get_object."""
    s3 = MagicMock()
    body_mock = MagicMock()
    body_mock.read.return_value = xml_content.encode("utf-8")
    s3.get_object.return_value = {"Body": body_mock}
    return s3


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def test_detect_format_drawio():
    assert _detect_format(DRAWIO_XML_BASIC) == "drawio"


def test_detect_format_lucidchart():
    assert _detect_format(LUCIDCHART_XML_BASIC) == "lucidchart"


# ---------------------------------------------------------------------------
# draw.io shape key extraction
# ---------------------------------------------------------------------------


def test_shape_key_prefers_resicon():
    style = "shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.ec2;fontSize=12;"
    assert _extract_drawio_shape_key(style) == "aws4.ec2"


def test_shape_key_falls_back_to_shape():
    style = "shape=mxgraph.aws4.s3;fontSize=12;"
    assert _extract_drawio_shape_key(style) == "aws4.s3"


def test_shape_key_non_aws_returns_none():
    style = "rounded=1;whiteSpace=wrap;html=1;"
    assert _extract_drawio_shape_key(style) is None


def test_shape_key_strips_mxgraph_prefix():
    style = "shape=mxgraph.aws4.vpc;"
    assert _extract_drawio_shape_key(style) == "aws4.vpc"


# ---------------------------------------------------------------------------
# draw.io parser — services
# ---------------------------------------------------------------------------


def test_parse_drawio_extracts_aws_services():
    services, _ = _parse_drawio(DRAWIO_XML_BASIC)
    ids = [s["id"] for s in services]
    assert "ec2-1" in ids
    assert "s3-1" in ids


def test_parse_drawio_ignores_non_aws_vertices():
    services, _ = _parse_drawio(DRAWIO_XML_BASIC)
    ids = [s["id"] for s in services]
    assert "rect-1" not in ids  # plain box with no AWS style


def test_parse_drawio_maps_known_shapes():
    services, _ = _parse_drawio(DRAWIO_XML_BASIC)
    by_id = {s["id"]: s for s in services}
    assert by_id["ec2-1"]["type"] == "aws_instance"
    assert by_id["s3-1"]["type"] == "aws_s3_bucket"


def test_parse_drawio_unknown_shape_type_is_unknown():
    services, _ = _parse_drawio(DRAWIO_XML_UNKNOWN_SHAPE)
    assert len(services) == 1
    assert services[0]["type"] == "unknown"


def test_parse_drawio_uses_cell_value_as_label():
    services, _ = _parse_drawio(DRAWIO_XML_BASIC)
    by_id = {s["id"]: s for s in services}
    assert by_id["ec2-1"]["label"] == "Web Server"
    assert by_id["s3-1"]["label"] == "Storage Bucket"


def test_parse_drawio_falls_back_to_id_when_no_value():
    xml = """
    <mxGraphModel><root>
      <mxCell id="0"/><mxCell id="1" parent="0"/>
      <mxCell id="lambda-99" value=""
              style="shape=mxgraph.aws4.lambda;" vertex="1" parent="1"/>
    </root></mxGraphModel>
    """
    services, _ = _parse_drawio(xml)
    assert services[0]["label"] == "lambda-99"


# ---------------------------------------------------------------------------
# draw.io parser — relationships
# ---------------------------------------------------------------------------


def test_parse_drawio_extracts_unlabeled_edge():
    _, relationships = _parse_drawio(DRAWIO_XML_BASIC)
    assert len(relationships) == 1
    edge = relationships[0]
    assert edge["source"] == "ec2-1"
    assert edge["target"] == "s3-1"
    assert edge["relationship_type"] == "connects_to"
    assert edge["label"] is None


def test_parse_drawio_labeled_edge_maps_relationship_type():
    _, relationships = _parse_drawio(DRAWIO_XML_LABELED_EDGE)
    assert len(relationships) == 1
    assert relationships[0]["relationship_type"] == "depends_on"
    assert relationships[0]["label"] == "depends on"


def test_parse_drawio_skips_edge_without_source_or_target():
    xml = """
    <mxGraphModel><root>
      <mxCell id="0"/><mxCell id="1" parent="0"/>
      <mxCell id="floating-edge" value="" edge="1" parent="1">
        <mxGeometry relative="1" as="geometry"/>
      </mxCell>
    </root></mxGraphModel>
    """
    _, relationships = _parse_drawio(xml)
    assert relationships == []


# ---------------------------------------------------------------------------
# Lucidchart parser — services
# ---------------------------------------------------------------------------


def test_parse_lucidchart_extracts_aws_elements():
    services, _ = _parse_lucidchart(LUCIDCHART_XML_BASIC)
    ids = [s["id"] for s in services]
    assert "elem-1" in ids
    assert "elem-2" in ids


def test_parse_lucidchart_ignores_non_aws_elements():
    services, _ = _parse_lucidchart(LUCIDCHART_XML_BASIC)
    ids = [s["id"] for s in services]
    assert "elem-3" not in ids


def test_parse_lucidchart_maps_known_types():
    services, _ = _parse_lucidchart(LUCIDCHART_XML_BASIC)
    by_id = {s["id"]: s for s in services}
    assert by_id["elem-1"]["type"] == "aws_instance"
    assert by_id["elem-2"]["type"] == "aws_db_instance"


def test_parse_lucidchart_uses_text_element_as_label():
    services, _ = _parse_lucidchart(LUCIDCHART_XML_BASIC)
    by_id = {s["id"]: s for s in services}
    assert by_id["elem-1"]["label"] == "App Server"
    assert by_id["elem-2"]["label"] == "Database"


def test_parse_lucidchart_falls_back_to_id_when_no_text():
    xml = """
    <drawing><page id="p1">
      <elements>
        <element id="bare-1" type="aws.S3"/>
      </elements>
    </page></drawing>
    """
    services, _ = _parse_lucidchart(xml)
    assert services[0]["label"] == "bare-1"


# ---------------------------------------------------------------------------
# Lucidchart parser — relationships
# ---------------------------------------------------------------------------


def test_parse_lucidchart_labeled_edge_maps_relationship_type():
    _, relationships = _parse_lucidchart(LUCIDCHART_XML_BASIC)
    assert len(relationships) == 1
    assert relationships[0]["source"] == "elem-1"
    assert relationships[0]["target"] == "elem-2"
    assert relationships[0]["relationship_type"] == "depends_on"
    assert relationships[0]["label"] == "depends on"


def test_parse_lucidchart_unlabeled_edge_defaults_to_connects_to():
    _, relationships = _parse_lucidchart(LUCIDCHART_XML_UNLABELED_EDGE)
    assert len(relationships) == 1
    assert relationships[0]["relationship_type"] == "connects_to"
    assert relationships[0]["label"] is None


# ---------------------------------------------------------------------------
# Network extraction
# ---------------------------------------------------------------------------


def test_extract_network_populates_vpcs_subnets_sgs():
    services, _ = _parse_drawio(DRAWIO_XML_NETWORK)
    network = _extract_network(services)
    assert len(network["vpcs"]) == 1
    assert network["vpcs"][0]["label"] == "Main VPC"
    assert len(network["subnets"]) == 1
    assert network["subnets"][0]["label"] == "Public Subnet"
    assert len(network["security_groups"]) == 1
    assert network["security_groups"][0]["label"] == "Web SG"


def test_extract_network_empty_when_no_network_resources():
    services = [{"id": "ec2-1", "type": "aws_instance", "label": "Web", "config": {}}]
    network = _extract_network(services)
    assert network == {"vpcs": [], "subnets": [], "security_groups": []}


def test_extract_network_vpc_cidr_from_config():
    services = [{
        "id": "vpc-1",
        "type": "aws_vpc",
        "label": "My VPC",
        "config": {"cidr_block": "10.0.0.0/16"},
    }]
    network = _extract_network(services)
    assert network["vpcs"][0]["cidr_block"] == "10.0.0.0/16"


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------


def test_build_manifest_all_entries_have_source_parsed():
    services = [
        {"id": "ec2-1", "type": "aws_instance", "label": "Web Server", "config": {}},
        {"id": "s3-1", "type": "aws_s3_bucket", "label": "Uploads", "config": {}},
    ]
    manifest = _build_manifest(services, ir_source="diagrams/arch.drawio")
    for param in manifest["parameters"]:
        assert param["source"] == "parsed"
        assert param["reasoning"] is None


def test_build_manifest_reasoning_is_null_for_parsed():
    services = [{"id": "x", "type": "aws_instance", "label": "host", "config": {}}]
    manifest = _build_manifest(services, ir_source="s3://bucket/key")
    for param in manifest["parameters"]:
        assert param["reasoning"] is None


def test_build_manifest_includes_diagram_id_parameter():
    services = [{"id": "ec2-99", "type": "aws_instance", "label": "App", "config": {}}]
    manifest = _build_manifest(services, ir_source="key")
    params = {p["parameter"]: p["value"] for p in manifest["parameters"]}
    assert "aws_instance.app.diagram_id" in params
    assert params["aws_instance.app.diagram_id"] == "ec2-99"


def test_build_manifest_config_values_included():
    services = [{
        "id": "vpc-1",
        "type": "aws_vpc",
        "label": "Main VPC",
        "config": {"cidr_block": "10.0.0.0/16"},
    }]
    manifest = _build_manifest(services, ir_source="key")
    params = {p["parameter"]: p["value"] for p in manifest["parameters"]}
    assert "aws_vpc.main_vpc.cidr_block" in params
    assert params["aws_vpc.main_vpc.cidr_block"] == "10.0.0.0/16"


def test_build_manifest_schema_version_is_set():
    manifest = _build_manifest([], ir_source="key")
    assert manifest["schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# Full Lambda handler — draw.io
# ---------------------------------------------------------------------------


def test_lambda_handler_drawio_returns_expected_keys():
    s3 = _mock_s3(DRAWIO_XML_BASIC)
    result = lambda_handler(_s3_event("my-bucket", "uploads/arch.drawio"), None, s3_client=s3)
    assert "ir_s3_key" in result
    assert "manifest_s3_key" in result
    assert "service_count" in result


def test_lambda_handler_drawio_service_count():
    s3 = _mock_s3(DRAWIO_XML_BASIC)
    result = lambda_handler(_s3_event("my-bucket", "uploads/arch.drawio"), None, s3_client=s3)
    assert result["service_count"] == 2  # ec2-1 and s3-1 (rect-1 is excluded)


def test_lambda_handler_drawio_writes_correct_s3_keys():
    s3 = _mock_s3(DRAWIO_XML_BASIC)
    result = lambda_handler(_s3_event("my-bucket", "uploads/arch.drawio"), None, s3_client=s3)
    assert result["ir_s3_key"] == "diagrams/arch/ir.json"
    assert result["manifest_s3_key"] == "diagrams/arch/manifest.json"


def test_lambda_handler_drawio_ir_structure():
    s3 = _mock_s3(DRAWIO_XML_BASIC)
    lambda_handler(_s3_event("my-bucket", "arch.drawio"), None, s3_client=s3)

    put_calls = {c.kwargs["Key"]: c for c in s3.put_object.call_args_list}
    ir = json.loads(put_calls["diagrams/arch/ir.json"].kwargs["Body"].decode())

    assert ir["schema_version"] == "1.0"
    assert ir["source_file"] == "arch.drawio"
    assert len(ir["services"]) == 2
    assert isinstance(ir["relationships"], list)
    assert "vpcs" in ir["network"]


def test_lambda_handler_drawio_manifest_structure():
    s3 = _mock_s3(DRAWIO_XML_BASIC)
    lambda_handler(_s3_event("my-bucket", "arch.drawio"), None, s3_client=s3)

    put_calls = {c.kwargs["Key"]: c for c in s3.put_object.call_args_list}
    manifest = json.loads(put_calls["diagrams/arch/manifest.json"].kwargs["Body"].decode())

    assert manifest["schema_version"] == "1.0"
    assert all(p["source"] == "parsed" for p in manifest["parameters"])


# ---------------------------------------------------------------------------
# Full Lambda handler — Lucidchart
# ---------------------------------------------------------------------------


def test_lambda_handler_lucidchart_service_count():
    s3 = _mock_s3(LUCIDCHART_XML_BASIC)
    result = lambda_handler(_s3_event("my-bucket", "charts/lc-arch.xml"), None, s3_client=s3)
    assert result["service_count"] == 2  # elem-3 (generic) is excluded


def test_lambda_handler_lucidchart_writes_correct_s3_keys():
    s3 = _mock_s3(LUCIDCHART_XML_BASIC)
    result = lambda_handler(_s3_event("my-bucket", "charts/lc-arch.xml"), None, s3_client=s3)
    assert result["ir_s3_key"] == "diagrams/lc-arch/ir.json"
    assert result["manifest_s3_key"] == "diagrams/lc-arch/manifest.json"


def test_lambda_handler_lucidchart_relationship_preserved():
    s3 = _mock_s3(LUCIDCHART_XML_BASIC)
    lambda_handler(_s3_event("my-bucket", "arch.xml"), None, s3_client=s3)

    put_calls = {c.kwargs["Key"]: c for c in s3.put_object.call_args_list}
    ir = json.loads(put_calls["diagrams/arch/ir.json"].kwargs["Body"].decode())

    assert len(ir["relationships"]) == 1
    assert ir["relationships"][0]["relationship_type"] == "depends_on"


# ---------------------------------------------------------------------------
# S3 key URL-decoding
# ---------------------------------------------------------------------------


def test_lambda_handler_decodes_url_encoded_key():
    s3 = _mock_s3(DRAWIO_XML_BASIC)
    # Simulate S3 URL-encoding spaces as '+'
    result = lambda_handler(
        _s3_event("my-bucket", "diagrams/my+arch.drawio"), None, s3_client=s3
    )
    assert result["ir_s3_key"] == "diagrams/my arch/ir.json"


# ---------------------------------------------------------------------------
# SHAPE_TO_TERRAFORM completeness smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape_key,expected_type", [
    ("aws4.ec2", "aws_instance"),
    ("aws4.s3", "aws_s3_bucket"),
    ("aws4.lambda", "aws_lambda_function"),
    ("aws4.rds", "aws_db_instance"),
    ("aws4.vpc", "aws_vpc"),
    ("aws4.subnet", "aws_subnet"),
    ("aws4.security_group", "aws_security_group"),
    ("aws4.dynamodb", "aws_dynamodb_table"),
    ("aws4.sns", "aws_sns_topic"),
    ("aws4.sqs", "aws_sqs_queue"),
    ("aws.ec2", "aws_instance"),
    ("aws.s3", "aws_s3_bucket"),
    ("aws.lambda", "aws_lambda_function"),
])
def test_shape_to_terraform_known_mappings(shape_key, expected_type):
    assert SHAPE_TO_TERRAFORM[shape_key] == expected_type
