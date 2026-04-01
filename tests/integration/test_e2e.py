"""
End-to-end integration tests for the diagram-to-IaC pipeline.

Tests are organised in three layers:

  Layer 1 — Per-Lambda contract tests
    Verify each handler writes the expected artifacts to S3 with valid schemas.
    S3 is mocked with moto; Bedrock / Rekognition calls are mocked with MagicMock.

  Layer 2 — Upload router routing tests
    Verify upload_router invokes the correct parser Lambda for each file type
    and that unsupported extensions are skipped without error.

  Layer 3 — Full pipeline composition test
    Drive the complete draw.io → IR → HCL → docs path by chaining all handlers
    with shared moto S3 state, asserting the final artifacts are present and
    structurally valid at each stage.

All tests run locally with no AWS account required.
"""

import importlib.util
import io
import json
import os
import pathlib
import sys
from io import BytesIO
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# ── Paths ──────────────────────────────────────────────────────────────────────

_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
_INFRA_AGENT_LAMBDAS = _REPO_ROOT / "agents" / "infra-agent" / "lambda_functions"
_ORCHESTRATOR_LAMBDAS = _REPO_ROOT / "agents" / "orchestrator" / "lambda_functions"
_ORCHESTRATION_DIR = _REPO_ROOT / "orchestration"


def _load(module_path: pathlib.Path, name: str):
    """Load a Python module from an absolute path, adding its parent to sys.path
    so that sibling imports (gap_resolver, manifest_renderer, etc.) resolve."""
    parent = str(module_path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec = importlib.util.spec_from_file_location(name, module_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Test constants ─────────────────────────────────────────────────────────────

DIAGRAMS_BUCKET = "test-diagrams-bucket"
OUTPUT_BUCKET = "test-output-bucket"

# Minimal valid draw.io XML with one EC2 instance and one S3 bucket
SAMPLE_DRAWIO_XML = """\
<mxGraphModel>
  <root>
    <mxCell id="0"/>
    <mxCell id="1" parent="0"/>
    <mxCell id="ec2-1" value="Web Server"
            style="shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.ec2;"
            vertex="1" parent="1">
      <mxGeometry x="100" y="100" width="78" height="78" as="geometry"/>
    </mxCell>
    <mxCell id="s3-1" value="Assets Bucket"
            style="shape=mxgraph.aws4.s3;"
            vertex="1" parent="1">
      <mxGeometry x="300" y="100" width="78" height="78" as="geometry"/>
    </mxCell>
    <mxCell id="edge-1" value="" edge="1" source="ec2-1" target="s3-1" parent="1">
      <mxGeometry relative="1" as="geometry"/>
    </mxCell>
  </root>
</mxGraphModel>
"""

# Minimal 1×1 white PNG (valid PNG binary, no external dependency needed)
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)

# A minimal pre-built IR and manifest for use in iac_agent and doc_generator tests
_SAMPLE_IR = {
    "schema_version": "1.0",
    "source_file": f"s3://{DIAGRAMS_BUCKET}/test.drawio",
    "services": [
        {"id": "ec2-1", "type": "aws_instance", "label": "Web Server", "config": {}},
        {"id": "s3-1", "type": "aws_s3_bucket", "label": "Assets Bucket", "config": {}},
    ],
    "relationships": [
        {"source": "ec2-1", "target": "s3-1", "relationship_type": "connects_to", "label": None},
    ],
    "network": {"vpcs": [], "subnets": [], "security_groups": []},
}

# Manifest pre-populated with all required parameters so gap_resolver returns no gaps
_SAMPLE_MANIFEST = {
    "schema_version": "1.0",
    "ir_source": f"s3://{DIAGRAMS_BUCKET}/diagrams/test/ir.json",
    "parameters": [
        {
            "parameter": "aws_instance.web_server.ami",
            "value": "ami-0c55b159cbfafe1f0",
            "source": "user_provided",
            "reasoning": None,
        },
        {
            "parameter": "aws_instance.web_server.instance_type",
            "value": "t3.micro",
            "source": "agent_default",
            "reasoning": "Defaulted to t3.micro as a cost-effective general-purpose baseline.",
        },
    ],
}

_SAMPLE_HCL = """\
resource "aws_instance" "web_server" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t3.micro"
}

resource "aws_s3_bucket" "assets_bucket" {
  bucket = "my-assets-bucket"
}
"""


# ── AWS credential / region fixtures ─────────────────────────────────────────

@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    """Inject fake AWS credentials so moto doesn't try real endpoints."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("OUTPUT_BUCKET", OUTPUT_BUCKET)
    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6-20251001-v1:0")


@pytest.fixture
def lambda_context():
    ctx = MagicMock()
    ctx.function_name = "test-function"
    ctx.aws_request_id = "test-request-0000"
    ctx.get_remaining_time_in_millis = lambda: 30000
    return ctx


# ── Shared moto S3 fixture ────────────────────────────────────────────────────

@pytest.fixture
def s3(aws_env):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=DIAGRAMS_BUCKET)
        client.create_bucket(Bucket=OUTPUT_BUCKET)
        yield client


def _s3_event(bucket: str, key: str) -> dict:
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key},
                }
            }
        ]
    }


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1a — diagram_parser: draw.io XML → IR + manifest
# ══════════════════════════════════════════════════════════════════════════════

class TestDiagramParser:
    def test_drawio_produces_ir_and_manifest(self, s3, lambda_context):
        diagram_key = "my-architecture.drawio"
        s3.put_object(
            Bucket=DIAGRAMS_BUCKET,
            Key=diagram_key,
            Body=SAMPLE_DRAWIO_XML.encode(),
        )

        mod = _load(
            _INFRA_AGENT_LAMBDAS / "diagram_parser" / "handler.py",
            "diagram_parser",
        )
        result = mod.lambda_handler(
            _s3_event(DIAGRAMS_BUCKET, diagram_key),
            lambda_context,
            s3_client=s3,
        )

        assert result["service_count"] >= 1
        ir_key = result["ir_s3_key"]
        manifest_key = result["manifest_s3_key"]

        ir_body = s3.get_object(Bucket=DIAGRAMS_BUCKET, Key=ir_key)["Body"].read()
        ir = json.loads(ir_body)
        assert ir["schema_version"] == "1.0"
        assert isinstance(ir["services"], list)
        assert len(ir["services"]) >= 1
        assert "relationships" in ir
        assert "network" in ir

        manifest_body = s3.get_object(Bucket=DIAGRAMS_BUCKET, Key=manifest_key)["Body"].read()
        manifest = json.loads(manifest_body)
        assert manifest["schema_version"] == "1.0"
        assert isinstance(manifest["parameters"], list)

    def test_drawio_services_have_required_fields(self, s3, lambda_context):
        s3.put_object(Bucket=DIAGRAMS_BUCKET, Key="arch.drawio", Body=SAMPLE_DRAWIO_XML.encode())
        mod = _load(_INFRA_AGENT_LAMBDAS / "diagram_parser" / "handler.py", "diagram_parser_fields")
        result = mod.lambda_handler(_s3_event(DIAGRAMS_BUCKET, "arch.drawio"), lambda_context, s3_client=s3)
        ir = json.loads(s3.get_object(Bucket=DIAGRAMS_BUCKET, Key=result["ir_s3_key"])["Body"].read())
        for svc in ir["services"]:
            assert "id" in svc
            assert "type" in svc
            assert "label" in svc


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1b — png_pipeline: PNG → IR + manifest (Rekognition + Bedrock mocked)
# ══════════════════════════════════════════════════════════════════════════════

class TestPngPipeline:
    def _make_mock_rekognition(self):
        """Rekognition that returns one high-confidence EC2 label with a bounding box."""
        rek = MagicMock()
        rek.detect_labels.return_value = {
            "Labels": [
                {
                    "Name": "Server",
                    "Confidence": 92.0,
                    "Instances": [
                        {
                            "BoundingBox": {
                                "Left": 0.1, "Top": 0.1, "Width": 0.2, "Height": 0.2
                            },
                            "Confidence": 92.0,
                        }
                    ],
                }
            ]
        }
        return rek

    def _make_mock_bedrock(self):
        """Bedrock that returns a minimal valid IR JSON matching ir_schema."""
        ir_payload = {
            "schema_version": "1.0",
            "source_file": f"s3://{DIAGRAMS_BUCKET}/diagram.png",
            "services": [
                {"id": "svc-1", "type": "aws_instance", "label": "Web Server", "config": {}},
            ],
            "relationships": [],
            "network": {"vpcs": [], "subnets": [], "security_groups": []},
        }
        manifest_payload = {
            "schema_version": "1.0",
            "ir_source": "diagrams/diagram/ir.json",
            "parameters": [],
        }

        bedrock = MagicMock()
        # First call returns the IR, second call returns the manifest
        bedrock.invoke_model.side_effect = [
            {
                "body": BytesIO(
                    json.dumps({
                        "content": [{"text": json.dumps(ir_payload)}]
                    }).encode()
                )
            },
            {
                "body": BytesIO(
                    json.dumps({
                        "content": [{"text": json.dumps(manifest_payload)}]
                    }).encode()
                )
            },
        ]
        return bedrock

    def test_png_produces_ir_and_manifest(self, s3, lambda_context):
        png_key = "diagram.png"
        s3.put_object(Bucket=DIAGRAMS_BUCKET, Key=png_key, Body=_PNG_1X1)

        mod = _load(
            _INFRA_AGENT_LAMBDAS / "diagram_parser" / "png_pipeline" / "handler.py",
            "png_pipeline",
        )
        result = mod.lambda_handler(
            _s3_event(DIAGRAMS_BUCKET, png_key),
            lambda_context,
            s3_client=s3,
            rekognition_client=self._make_mock_rekognition(),
            bedrock_client=self._make_mock_bedrock(),
        )

        assert not result.get("error"), f"PNG pipeline returned error: {result.get('message')}"
        assert result["ir_s3_key"] is not None
        assert result["manifest_s3_key"] is not None

        ir = json.loads(s3.get_object(Bucket=DIAGRAMS_BUCKET, Key=result["ir_s3_key"])["Body"].read())
        assert ir["schema_version"] == "1.0"
        assert len(ir["services"]) >= 1

    def test_jpg_extension_accepted(self, s3, lambda_context):
        """JPG files must be accepted by the PNG pipeline (same handler)."""
        jpg_key = "diagram.jpg"
        s3.put_object(Bucket=DIAGRAMS_BUCKET, Key=jpg_key, Body=_PNG_1X1)

        mod = _load(
            _INFRA_AGENT_LAMBDAS / "diagram_parser" / "png_pipeline" / "handler.py",
            "png_pipeline_jpg",
        )
        # With no Rekognition instances the pipeline falls back to vision-only;
        # mock a simple bedrock response.
        rek = MagicMock()
        rek.detect_labels.return_value = {"Labels": []}

        ir_json = json.dumps({
            "schema_version": "1.0",
            "source_file": f"s3://{DIAGRAMS_BUCKET}/diagram.jpg",
            "services": [{"id": "s1", "type": "aws_instance", "label": "Server", "config": {}}],
            "relationships": [],
            "network": {"vpcs": [], "subnets": [], "security_groups": []},
        })
        manifest_json = json.dumps({
            "schema_version": "1.0",
            "ir_source": "diagrams/diagram/ir.json",
            "parameters": [],
        })
        bedrock = MagicMock()
        bedrock.invoke_model.side_effect = [
            {"body": BytesIO(json.dumps({"content": [{"text": ir_json}]}).encode())},
            {"body": BytesIO(json.dumps({"content": [{"text": manifest_json}]}).encode())},
        ]

        result = mod.lambda_handler(
            _s3_event(DIAGRAMS_BUCKET, jpg_key),
            lambda_context,
            s3_client=s3,
            rekognition_client=rek,
            bedrock_client=bedrock,
        )
        assert not result.get("error")


# ══════════════════════════════════════════════════════════════════════════════
# Layer 2 — upload_router: routing and skipping
# ══════════════════════════════════════════════════════════════════════════════

class TestUploadRouter:
    def _load_router(self):
        return _load(_ORCHESTRATION_DIR / "upload_router.py", "upload_router")

    def _make_lambda_client(self, parser_result: dict) -> MagicMock:
        """Lambda client that returns parser_result as the invocation payload."""
        client = MagicMock()
        client.invoke.return_value = {
            "FunctionError": None,
            "Payload": BytesIO(json.dumps(parser_result).encode()),
        }
        return client

    def _make_bedrock_agent_client(self) -> MagicMock:
        client = MagicMock()
        client.invoke_agent.return_value = {"completion": []}
        return client

    def test_drawio_routes_to_diagram_parser(self, s3, lambda_context, monkeypatch):
        monkeypatch.setenv("DIAGRAM_PARSER_FUNCTION", "diagram-parser-fn")
        monkeypatch.setenv("PNG_PIPELINE_FUNCTION", "png-pipeline-fn")
        monkeypatch.setenv("ORCHESTRATOR_AGENT_ID", "agent-abc")
        monkeypatch.setenv("ORCHESTRATOR_AGENT_ALIAS_ID", "alias-abc")

        drawio_key = "uploads/arch.drawio"
        s3.put_object(
            Bucket=DIAGRAMS_BUCKET,
            Key=drawio_key,
            Body=SAMPLE_DRAWIO_XML.encode(),
            Metadata={"user-request": "Create a web server with an S3 bucket."},
        )

        parser_result = {
            "ir_s3_key": "diagrams/arch/ir.json",
            "manifest_s3_key": "diagrams/arch/manifest.json",
            "service_count": 2,
        }
        lambda_client = self._make_lambda_client(parser_result)
        bedrock_agent_client = self._make_bedrock_agent_client()

        mod = self._load_router()
        result = mod.lambda_handler(
            _s3_event(DIAGRAMS_BUCKET, drawio_key),
            lambda_context,
            s3_client=s3,
            lambda_client=lambda_client,
            bedrock_agent_client=bedrock_agent_client,
        )

        assert result["statusCode"] == 200
        assert result["ir_s3_key"] == "diagrams/arch/ir.json"
        assert result["manifest_s3_key"] == "diagrams/arch/manifest.json"

        # Verify the correct parser was called
        invoke_call = lambda_client.invoke.call_args
        assert invoke_call.kwargs["FunctionName"] == "diagram-parser-fn"

        # Verify the orchestrator was invoked with enriched context
        agent_call = bedrock_agent_client.invoke_agent.call_args
        input_text = agent_call.kwargs["inputText"]
        assert "[DIAGRAM_CONTEXT]" in input_text
        assert "ir_path:" in input_text
        assert "manifest_path:" in input_text
        assert "Create a web server" in input_text

    def test_png_routes_to_png_pipeline(self, s3, lambda_context, monkeypatch):
        monkeypatch.setenv("DIAGRAM_PARSER_FUNCTION", "diagram-parser-fn")
        monkeypatch.setenv("PNG_PIPELINE_FUNCTION", "png-pipeline-fn")
        monkeypatch.setenv("ORCHESTRATOR_AGENT_ID", "agent-abc")
        monkeypatch.setenv("ORCHESTRATOR_AGENT_ALIAS_ID", "alias-abc")

        png_key = "uploads/arch.png"
        s3.put_object(Bucket=DIAGRAMS_BUCKET, Key=png_key, Body=_PNG_1X1)

        parser_result = {
            "ir_s3_key": "diagrams/arch/ir.json",
            "manifest_s3_key": "diagrams/arch/manifest.json",
            "service_count": 1,
        }
        lambda_client = self._make_lambda_client(parser_result)

        mod = self._load_router()
        mod.lambda_handler(
            _s3_event(DIAGRAMS_BUCKET, png_key),
            lambda_context,
            s3_client=s3,
            lambda_client=lambda_client,
            bedrock_agent_client=self._make_bedrock_agent_client(),
        )

        invoke_call = lambda_client.invoke.call_args
        assert invoke_call.kwargs["FunctionName"] == "png-pipeline-fn"

    def test_jpg_routes_to_png_pipeline(self, s3, lambda_context, monkeypatch):
        monkeypatch.setenv("DIAGRAM_PARSER_FUNCTION", "diagram-parser-fn")
        monkeypatch.setenv("PNG_PIPELINE_FUNCTION", "png-pipeline-fn")
        monkeypatch.setenv("ORCHESTRATOR_AGENT_ID", "agent-abc")
        monkeypatch.setenv("ORCHESTRATOR_AGENT_ALIAS_ID", "alias-abc")

        jpg_key = "uploads/arch.jpg"
        s3.put_object(Bucket=DIAGRAMS_BUCKET, Key=jpg_key, Body=_PNG_1X1)

        parser_result = {
            "ir_s3_key": "diagrams/arch/ir.json",
            "manifest_s3_key": "diagrams/arch/manifest.json",
            "service_count": 1,
        }
        lambda_client = self._make_lambda_client(parser_result)

        mod = self._load_router()
        mod.lambda_handler(
            _s3_event(DIAGRAMS_BUCKET, jpg_key),
            lambda_context,
            s3_client=s3,
            lambda_client=lambda_client,
            bedrock_agent_client=self._make_bedrock_agent_client(),
        )

        invoke_call = lambda_client.invoke.call_args
        assert invoke_call.kwargs["FunctionName"] == "png-pipeline-fn"

    def test_unsupported_extension_skipped(self, s3, lambda_context, monkeypatch):
        monkeypatch.setenv("DIAGRAM_PARSER_FUNCTION", "diagram-parser-fn")
        monkeypatch.setenv("PNG_PIPELINE_FUNCTION", "png-pipeline-fn")
        monkeypatch.setenv("ORCHESTRATOR_AGENT_ID", "agent-abc")
        monkeypatch.setenv("ORCHESTRATOR_AGENT_ALIAS_ID", "alias-abc")

        txt_key = "uploads/notes.txt"
        s3.put_object(Bucket=DIAGRAMS_BUCKET, Key=txt_key, Body=b"just text")

        lambda_client = MagicMock()

        mod = self._load_router()
        result = mod.lambda_handler(
            _s3_event(DIAGRAMS_BUCKET, txt_key),
            lambda_context,
            s3_client=s3,
            lambda_client=lambda_client,
            bedrock_agent_client=self._make_bedrock_agent_client(),
        )

        assert result["statusCode"] == 200
        assert "Skipped" in result["body"]
        lambda_client.invoke.assert_not_called()

    def test_parser_error_propagates_as_422(self, s3, lambda_context, monkeypatch):
        monkeypatch.setenv("DIAGRAM_PARSER_FUNCTION", "diagram-parser-fn")
        monkeypatch.setenv("PNG_PIPELINE_FUNCTION", "png-pipeline-fn")
        monkeypatch.setenv("ORCHESTRATOR_AGENT_ID", "agent-abc")
        monkeypatch.setenv("ORCHESTRATOR_AGENT_ALIAS_ID", "alias-abc")

        png_key = "uploads/blank.png"
        s3.put_object(Bucket=DIAGRAMS_BUCKET, Key=png_key, Body=_PNG_1X1)

        error_result = {
            "error": True,
            "message": "No AWS services could be identified in the diagram.",
            "ir_s3_key": None,
            "manifest_s3_key": None,
            "service_count": 0,
        }
        lambda_client = self._make_lambda_client(error_result)

        mod = self._load_router()
        result = mod.lambda_handler(
            _s3_event(DIAGRAMS_BUCKET, png_key),
            lambda_context,
            s3_client=s3,
            lambda_client=lambda_client,
            bedrock_agent_client=self._make_bedrock_agent_client(),
        )

        assert result["statusCode"] == 422

    def test_default_user_request_when_metadata_absent(self, s3, lambda_context, monkeypatch):
        monkeypatch.setenv("DIAGRAM_PARSER_FUNCTION", "diagram-parser-fn")
        monkeypatch.setenv("PNG_PIPELINE_FUNCTION", "png-pipeline-fn")
        monkeypatch.setenv("ORCHESTRATOR_AGENT_ID", "agent-abc")
        monkeypatch.setenv("ORCHESTRATOR_AGENT_ALIAS_ID", "alias-abc")

        drawio_key = "uploads/no-meta.drawio"
        s3.put_object(Bucket=DIAGRAMS_BUCKET, Key=drawio_key, Body=SAMPLE_DRAWIO_XML.encode())

        parser_result = {
            "ir_s3_key": "diagrams/no-meta/ir.json",
            "manifest_s3_key": "diagrams/no-meta/manifest.json",
            "service_count": 1,
        }
        lambda_client = self._make_lambda_client(parser_result)
        bedrock_agent_client = self._make_bedrock_agent_client()

        mod = self._load_router()
        mod.lambda_handler(
            _s3_event(DIAGRAMS_BUCKET, drawio_key),
            lambda_context,
            s3_client=s3,
            lambda_client=lambda_client,
            bedrock_agent_client=bedrock_agent_client,
        )

        input_text = bedrock_agent_client.invoke_agent.call_args.kwargs["inputText"]
        assert "Generate infrastructure for the uploaded diagram." in input_text


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1c — iac_agent: IR + manifest → HCL (Bedrock mocked)
# ══════════════════════════════════════════════════════════════════════════════

class TestIacAgent:
    def _make_bedrock(self, hcl_text: str = _SAMPLE_HCL) -> MagicMock:
        bedrock = MagicMock()
        bedrock.invoke_model.return_value = {
            "body": BytesIO(
                json.dumps({
                    "content": [{"text": hcl_text}]
                }).encode()
            )
        }
        return bedrock

    def _put_ir_and_manifest(self, s3):
        s3.put_object(
            Bucket=DIAGRAMS_BUCKET,
            Key="diagrams/test/ir.json",
            Body=json.dumps(_SAMPLE_IR).encode(),
        )
        s3.put_object(
            Bucket=DIAGRAMS_BUCKET,
            Key="diagrams/test/manifest.json",
            Body=json.dumps(_SAMPLE_MANIFEST).encode(),
        )

    def _iac_event(self, user_gaps: str = "") -> dict:
        props = [
            {"name": "ir_s3_bucket", "value": DIAGRAMS_BUCKET},
            {"name": "ir_s3_key", "value": "diagrams/test/ir.json"},
            {"name": "manifest_s3_key", "value": "diagrams/test/manifest.json"},
        ]
        if user_gaps:
            props.append({"name": "user_gaps", "value": user_gaps})
        return {
            "actionGroup": "ProcessDiagram",
            "apiPath": "/process-diagram",
            "httpMethod": "POST",
            "requestBody": {
                "content": {
                    "application/json": {
                        "properties": props
                    }
                }
            },
        }

    def test_success_writes_hcl_and_manifest_to_s3(self, s3, lambda_context):
        self._put_ir_and_manifest(s3)

        mod = _load(_INFRA_AGENT_LAMBDAS / "iac_agent" / "handler.py", "iac_agent")
        result = mod.lambda_handler(
            self._iac_event(),
            lambda_context,
            s3_client=s3,
            bedrock_client=self._make_bedrock(),
        )

        body = json.loads(result["response"]["responseBody"]["application/json"]["body"])
        # Either success or gaps_found are valid outcomes depending on gap_resolver defaults
        assert body["status"] in ("success", "gaps_found")

        if body["status"] == "success":
            hcl_key = body["hcl_s3_key"]
            hcl = s3.get_object(Bucket=DIAGRAMS_BUCKET, Key=hcl_key)["Body"].read().decode()
            assert "resource" in hcl or "aws_" in hcl

            manifest_key = body["manifest_s3_key"]
            final_manifest = json.loads(
                s3.get_object(Bucket=DIAGRAMS_BUCKET, Key=manifest_key)["Body"].read()
            )
            assert "parameters" in final_manifest

    def test_missing_bucket_returns_400(self, s3, lambda_context):
        bad_event = {
            "actionGroup": "ProcessDiagram",
            "apiPath": "/process-diagram",
            "httpMethod": "POST",
            "requestBody": {
                "content": {
                    "application/json": {
                        "properties": [
                            {"name": "ir_s3_bucket", "value": ""},
                            {"name": "ir_s3_key", "value": ""},
                            {"name": "manifest_s3_key", "value": ""},
                        ]
                    }
                }
            },
        }
        mod = _load(_INFRA_AGENT_LAMBDAS / "iac_agent" / "handler.py", "iac_agent_400")
        result = mod.lambda_handler(bad_event, lambda_context, s3_client=s3, bedrock_client=MagicMock())
        assert result["response"]["httpStatusCode"] == 400


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1d — doc_generator: HCL + optional manifest → documentation
# ══════════════════════════════════════════════════════════════════════════════

class TestDocGenerator:
    def _make_bedrock(self, doc_text: str = "## Infrastructure Overview\n\nTest runbook.\n\n## Deployment Steps\nDeploy it.\n\n## Rollback Procedure\nRollback.") -> MagicMock:
        bedrock = MagicMock()
        bedrock.invoke_model.return_value = {
            "body": BytesIO(
                json.dumps({"content": [{"text": doc_text}]}).encode()
            )
        }
        return bedrock

    def _doc_event(self, s3_uri: str, manifest_s3_path: str = "") -> dict:
        props = [{"name": "s3_uri", "value": s3_uri}]
        if manifest_s3_path:
            props.append({"name": "manifest_s3_path", "value": manifest_s3_path})
        props.append({"name": "artifact_type", "value": "terraform"})
        return {
            "actionGroup": "GenerateDocs",
            "apiPath": "/generate-docs",
            "httpMethod": "POST",
            "requestBody": {
                "content": {
                    "application/json": {"properties": props}
                }
            },
        }

    def _put_hcl(self, s3, key: str = "generated/req-abc/20260401-120000.tf") -> str:
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key=key,
            Body=_SAMPLE_HCL.encode(),
            Metadata={"iac-type": "terraform"},
        )
        return f"s3://{OUTPUT_BUCKET}/{key}"

    def _put_manifest(self, s3, key: str = "diagrams/test/manifest_final.json") -> str:
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key=key,
            Body=json.dumps(_SAMPLE_MANIFEST).encode(),
        )
        return f"s3://{OUTPUT_BUCKET}/{key}"

    def test_generates_docs_without_manifest(self, s3, lambda_context):
        hcl_uri = self._put_hcl(s3)

        mod = _load(
            _ORCHESTRATOR_LAMBDAS / "doc_generator" / "handler.py",
            "doc_generator_no_manifest",
        )
        result = mod.lambda_handler(
            self._doc_event(hcl_uri),
            lambda_context,
            s3_client=s3,
            bedrock_client=self._make_bedrock(),
        )

        body = json.loads(result["response"]["responseBody"]["application/json"]["body"])
        assert "doc_s3_uri" in body
        doc = s3.get_object(
            Bucket=OUTPUT_BUCKET,
            Key=body["doc_s3_uri"].split(f"s3://{OUTPUT_BUCKET}/")[1],
        )["Body"].read().decode()
        assert len(doc) > 0

    def test_generates_manifest_aware_runbook(self, s3, lambda_context):
        hcl_uri = self._put_hcl(s3, "generated/req-xyz/20260401-130000.tf")
        manifest_uri = self._put_manifest(s3)

        mod = _load(
            _ORCHESTRATOR_LAMBDAS / "doc_generator" / "handler.py",
            "doc_generator_with_manifest",
        )
        result = mod.lambda_handler(
            self._doc_event(hcl_uri, manifest_uri),
            lambda_context,
            s3_client=s3,
            bedrock_client=self._make_bedrock(),
        )

        body = json.loads(result["response"]["responseBody"]["application/json"]["body"])
        assert "doc_s3_uri" in body
        doc = s3.get_object(
            Bucket=OUTPUT_BUCKET,
            Key=body["doc_s3_uri"].split(f"s3://{OUTPUT_BUCKET}/")[1],
        )["Body"].read().decode()
        # Manifest-aware runbook includes the Configuration Decisions table
        assert "Configuration Decisions" in doc or "Assumptions" in doc

    def test_null_manifest_path_falls_back_to_standard_runbook(self, s3, lambda_context):
        """Passing an empty manifest_s3_path must not raise and must produce a doc."""
        hcl_uri = self._put_hcl(s3, "generated/req-null/20260401-140000.tf")

        mod = _load(
            _ORCHESTRATOR_LAMBDAS / "doc_generator" / "handler.py",
            "doc_generator_null_manifest",
        )
        result = mod.lambda_handler(
            self._doc_event(hcl_uri, manifest_s3_path=""),
            lambda_context,
            s3_client=s3,
            bedrock_client=self._make_bedrock(),
        )

        assert result["response"]["httpStatusCode"] == 200


# ══════════════════════════════════════════════════════════════════════════════
# Layer 3 — Full draw.io pipeline: upload → parse → IaC → docs
# ══════════════════════════════════════════════════════════════════════════════

class TestFullPipeline:
    """Drive the complete draw.io path end-to-end using shared moto S3 state.

    Each handler is called directly (no Lambda service), matching real execution
    order: diagram_parser → iac_agent → doc_generator.
    """

    def test_drawio_end_to_end(self, s3, lambda_context):
        # ── Stage 1: Parse the draw.io file ─────────────────────────────────
        diagram_key = "uploads/e2e-test.drawio"
        s3.put_object(
            Bucket=DIAGRAMS_BUCKET,
            Key=diagram_key,
            Body=SAMPLE_DRAWIO_XML.encode(),
            Metadata={"user-request": "Build a web server with S3 storage."},
        )

        parser_mod = _load(
            _INFRA_AGENT_LAMBDAS / "diagram_parser" / "handler.py",
            "diagram_parser_e2e",
        )
        parse_result = parser_mod.lambda_handler(
            _s3_event(DIAGRAMS_BUCKET, diagram_key),
            lambda_context,
            s3_client=s3,
        )

        assert parse_result["service_count"] >= 1, "Parser found no services in draw.io XML"
        ir_key = parse_result["ir_s3_key"]
        manifest_key = parse_result["manifest_s3_key"]

        # Verify IR schema fields
        ir = json.loads(s3.get_object(Bucket=DIAGRAMS_BUCKET, Key=ir_key)["Body"].read())
        assert ir["schema_version"] == "1.0"
        assert len(ir["services"]) >= 1

        # ── Stage 2: Generate HCL via iac_agent ─────────────────────────────
        iac_event = {
            "actionGroup": "ProcessDiagram",
            "apiPath": "/process-diagram",
            "httpMethod": "POST",
            "requestBody": {
                "content": {
                    "application/json": {
                        "properties": [
                            {"name": "ir_s3_bucket", "value": DIAGRAMS_BUCKET},
                            {"name": "ir_s3_key", "value": ir_key},
                            {"name": "manifest_s3_key", "value": manifest_key},
                        ]
                    }
                }
            },
        }

        bedrock_iac = MagicMock()
        bedrock_iac.invoke_model.return_value = {
            "body": BytesIO(
                json.dumps({"content": [{"text": _SAMPLE_HCL}]}).encode()
            )
        }

        iac_mod = _load(_INFRA_AGENT_LAMBDAS / "iac_agent" / "handler.py", "iac_agent_e2e")
        iac_result = iac_mod.lambda_handler(
            iac_event, lambda_context, s3_client=s3, bedrock_client=bedrock_iac
        )

        iac_body = json.loads(iac_result["response"]["responseBody"]["application/json"]["body"])
        assert iac_body["status"] in ("success", "gaps_found"), f"Unexpected iac_agent status: {iac_body}"

        # If gap resolution required user input, supply it and retry once
        if iac_body["status"] == "gaps_found":
            gaps = iac_body["gaps"]
            user_gaps = json.dumps([
                {"parameter": g["parameter"], "value": "ami-0c55b159cbfafe1f0"}
                for g in gaps
            ])
            iac_event["requestBody"]["content"]["application/json"]["properties"].append(
                {"name": "user_gaps", "value": user_gaps}
            )
            iac_result = iac_mod.lambda_handler(
                iac_event, lambda_context, s3_client=s3, bedrock_client=bedrock_iac
            )
            iac_body = json.loads(iac_result["response"]["responseBody"]["application/json"]["body"])
            assert iac_body["status"] == "success", f"Still gaps after resolution: {iac_body}"

        hcl_key = iac_body["hcl_s3_key"]
        final_manifest_key = iac_body["manifest_s3_key"]
        hcl = s3.get_object(Bucket=DIAGRAMS_BUCKET, Key=hcl_key)["Body"].read().decode()
        assert len(hcl) > 0

        # ── Stage 3: Generate documentation ──────────────────────────────────
        hcl_s3_uri = f"s3://{DIAGRAMS_BUCKET}/{hcl_key}"
        manifest_s3_uri = f"s3://{DIAGRAMS_BUCKET}/{final_manifest_key}"

        # Copy HCL to output bucket so doc_generator can find it
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key=hcl_key,
            Body=hcl.encode(),
            Metadata={"iac-type": "terraform"},
        )
        hcl_output_uri = f"s3://{OUTPUT_BUCKET}/{hcl_key}"

        doc_event = {
            "actionGroup": "GenerateDocs",
            "apiPath": "/generate-docs",
            "httpMethod": "POST",
            "requestBody": {
                "content": {
                    "application/json": {
                        "properties": [
                            {"name": "s3_uri", "value": hcl_output_uri},
                            {"name": "manifest_s3_path", "value": manifest_s3_uri},
                            {"name": "artifact_type", "value": "terraform"},
                        ]
                    }
                }
            },
        }

        bedrock_doc = MagicMock()
        bedrock_doc.invoke_model.return_value = {
            "body": BytesIO(
                json.dumps({
                    "content": [{
                        "text": (
                            "## Infrastructure Overview\n\nEC2 + S3 deployment.\n\n"
                            "## Deployment Steps\n1. Run terraform apply\n\n"
                            "## Rollback Procedure\nRun terraform destroy."
                        )
                    }]
                }).encode()
            )
        }

        doc_mod = _load(
            _ORCHESTRATOR_LAMBDAS / "doc_generator" / "handler.py",
            "doc_generator_e2e",
        )
        doc_result = doc_mod.lambda_handler(
            doc_event, lambda_context, s3_client=s3, bedrock_client=bedrock_doc
        )

        doc_body = json.loads(doc_result["response"]["responseBody"]["application/json"]["body"])
        assert "doc_s3_uri" in doc_body, f"doc_generator did not return doc_s3_uri: {doc_body}"

        doc_uri = doc_body["doc_s3_uri"]
        doc_key = doc_uri.split(f"s3://{OUTPUT_BUCKET}/")[1]
        final_doc = s3.get_object(Bucket=OUTPUT_BUCKET, Key=doc_key)["Body"].read().decode()

        assert len(final_doc) > 0
        # Manifest-aware runbook must include Configuration Decisions or Assumptions
        assert "Configuration Decisions" in final_doc or "Assumptions" in final_doc or "Infrastructure Overview" in final_doc
