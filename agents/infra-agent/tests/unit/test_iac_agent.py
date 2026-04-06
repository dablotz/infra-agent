"""Unit tests for iac_agent/handler.py.

Covers error paths and key behaviour not exercised by the integration suite:
  - Missing required properties → 400
  - S3 load failure → 500
  - Malformed user_gaps JSON → 400
  - user_gaps merged into manifest (append-only, no duplicates)
  - Unresolvable gaps → 200 gaps_found response
  - Missing BEDROCK_MODEL_ID env var → 500
  - Bedrock invocation failure → 500
  - S3 write failure → 500
  - Success: HCL key and manifest key derived correctly from ir_key
  - Success: response envelope fields correct
  - Nova model response parsing path
  - Markdown fence stripping from Bedrock response
"""

import importlib.util
import io
import json
import pathlib
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Load handler
# ---------------------------------------------------------------------------
_path = (
    pathlib.Path(__file__).parent.parent.parent
    / "lambda_functions"
    / "iac_agent"
    / "handler.py"
)
_spec = importlib.util.spec_from_file_location("iac_agent_handler", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

lambda_handler = _mod.lambda_handler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BUCKET = "test-infra-bucket"
IR_KEY = "diagrams/my_arch/ir.json"
MANIFEST_KEY = "diagrams/my_arch/manifest.json"

# Minimal IR with only an S3 bucket service — no required params, so no gaps.
_SIMPLE_IR = {
    "schema_version": "1.0",
    "source_file": f"s3://{BUCKET}/diagram.drawio",
    "services": [
        {"id": "b1", "type": "aws_s3_bucket", "label": "assets", "config": {}},
    ],
    "relationships": [],
    "network": {"vpcs": [], "subnets": [], "security_groups": []},
}

_SIMPLE_MANIFEST = {
    "schema_version": "1.0",
    "ir_source": f"s3://{BUCKET}/{IR_KEY}",
    "parameters": [],
}

SAMPLE_HCL = 'resource "aws_s3_bucket" "assets" {}\n'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _s3_body(data: dict) -> dict:
    """Return a mock S3 get_object response whose Body reads JSON."""
    body = MagicMock()
    body.read.return_value = json.dumps(data).encode("utf-8")
    return {"Body": body}


def _make_s3_mock(ir=None, manifest=None):
    """Return an S3 mock that yields IR then manifest on successive get_object calls."""
    s3 = MagicMock()
    s3.get_object.side_effect = [
        _s3_body(ir if ir is not None else _SIMPLE_IR),
        _s3_body(manifest if manifest is not None else _SIMPLE_MANIFEST),
    ]
    s3.put_object.return_value = {}
    return s3


def _make_bedrock_mock(hcl_text=SAMPLE_HCL, *, nova=False):
    """Return a Bedrock mock that returns hcl_text as a Claude or Nova response."""
    bedrock = MagicMock()
    body = MagicMock()
    if nova:
        payload = {"output": {"message": {"content": [{"text": hcl_text}]}}}
    else:
        payload = {"content": [{"text": hcl_text}]}
    body.read.return_value = json.dumps(payload).encode("utf-8")
    bedrock.invoke_model.return_value = {"body": body}
    return bedrock


def _make_event(ir_bucket=BUCKET, ir_key=IR_KEY, manifest_key=MANIFEST_KEY, user_gaps=None):
    props = [
        {"name": "ir_s3_bucket", "type": "string", "value": ir_bucket},
        {"name": "ir_s3_key", "type": "string", "value": ir_key},
        {"name": "manifest_s3_key", "type": "string", "value": manifest_key},
    ]
    if user_gaps is not None:
        props.append({"name": "user_gaps", "type": "string", "value": user_gaps})
    return {
        "actionGroup": "IaCAgent",
        "apiPath": "/generate-iac",
        "httpMethod": "POST",
        "requestBody": {"content": {"application/json": {"properties": props}}},
    }


def _body(result: dict) -> dict:
    return json.loads(result["response"]["responseBody"]["application/json"]["body"])


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0")


# ---------------------------------------------------------------------------
# Bedrock action group envelope
# ---------------------------------------------------------------------------


def test_returns_bedrock_envelope(lambda_context):
    result = lambda_handler(
        _make_event(), lambda_context,
        s3_client=_make_s3_mock(),
        bedrock_client=_make_bedrock_mock(),
    )

    assert result["messageVersion"] == "1.0"
    assert result["response"]["actionGroup"] == "IaCAgent"
    assert result["response"]["httpStatusCode"] == 200


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_missing_ir_s3_bucket_returns_400(lambda_context):
    event = _make_event(ir_bucket="")
    result = lambda_handler(event, lambda_context, s3_client=MagicMock())

    assert result["response"]["httpStatusCode"] == 400
    assert "ir_s3_bucket" in _body(result)["error"]


def test_missing_ir_s3_key_returns_400(lambda_context):
    event = _make_event(ir_key="")
    result = lambda_handler(event, lambda_context, s3_client=MagicMock())

    assert result["response"]["httpStatusCode"] == 400


def test_missing_manifest_s3_key_returns_400(lambda_context):
    event = _make_event(manifest_key="")
    result = lambda_handler(event, lambda_context, s3_client=MagicMock())

    assert result["response"]["httpStatusCode"] == 400


# ---------------------------------------------------------------------------
# S3 load failure
# ---------------------------------------------------------------------------


def test_s3_load_failure_returns_500(lambda_context):
    s3 = MagicMock()
    s3.get_object.side_effect = RuntimeError("NoSuchKey")
    result = lambda_handler(
        _make_event(), lambda_context, s3_client=s3, bedrock_client=MagicMock()
    )

    assert result["response"]["httpStatusCode"] == 500
    assert "S3" in _body(result)["error"]


# ---------------------------------------------------------------------------
# user_gaps handling
# ---------------------------------------------------------------------------


def test_malformed_user_gaps_json_returns_400(lambda_context):
    result = lambda_handler(
        _make_event(user_gaps="not-valid-json"),
        lambda_context,
        s3_client=_make_s3_mock(),
        bedrock_client=_make_bedrock_mock(),
    )

    assert result["response"]["httpStatusCode"] == 400
    assert "user_gaps" in _body(result)["error"]


def test_user_gaps_are_merged_into_manifest(lambda_context, monkeypatch):
    """User-resolved gaps must be appended to the manifest before gap resolution."""
    captured_manifest = {}

    original_resolve = _mod.resolve_gaps

    def capturing_resolve(ir, manifest):
        captured_manifest.update(manifest)
        return original_resolve(ir, manifest)

    monkeypatch.setattr(_mod, "resolve_gaps", capturing_resolve)

    user_gaps = json.dumps([{"parameter": "aws_instance.web.ami", "value": "ami-0abc1234"}])
    result = lambda_handler(
        _make_event(user_gaps=user_gaps),
        lambda_context,
        s3_client=_make_s3_mock(),
        bedrock_client=_make_bedrock_mock(),
    )

    merged_params = captured_manifest.get("parameters", [])
    merged_keys = [p["parameter"] for p in merged_params]
    assert "aws_instance.web.ami" in merged_keys


def test_user_gaps_not_duplicated_when_already_in_manifest(lambda_context, monkeypatch):
    """A user_gap whose parameter is already in the manifest must not be added again."""
    existing_manifest = {
        **_SIMPLE_MANIFEST,
        "parameters": [
            {"parameter": "aws_vpc.main.cidr_block", "value": "10.0.0.0/16",
             "source": "parsed", "reasoning": None},
        ],
    }
    captured_manifest = {}

    original_resolve = _mod.resolve_gaps

    def capturing_resolve(ir, manifest):
        captured_manifest.update({"parameters": list(manifest["parameters"])})
        return original_resolve(ir, manifest)

    monkeypatch.setattr(_mod, "resolve_gaps", capturing_resolve)

    # Try to merge a duplicate of the param already in the manifest
    user_gaps = json.dumps([{"parameter": "aws_vpc.main.cidr_block", "value": "172.16.0.0/12"}])
    lambda_handler(
        _make_event(user_gaps=user_gaps),
        lambda_context,
        s3_client=_make_s3_mock(manifest=existing_manifest),
        bedrock_client=_make_bedrock_mock(),
    )

    cidr_entries = [
        p for p in captured_manifest.get("parameters", [])
        if p["parameter"] == "aws_vpc.main.cidr_block"
    ]
    assert len(cidr_entries) == 1


# ---------------------------------------------------------------------------
# Gap resolution → gaps_found response
# ---------------------------------------------------------------------------


def test_unresolvable_gaps_return_gaps_found_status(lambda_context):
    """An IR with an aws_instance missing AMI must produce gaps_found, not 200 success."""
    ir_with_gap = {
        **_SIMPLE_IR,
        "services": [
            {"id": "i1", "type": "aws_instance", "label": "web", "config": {}},
        ],
    }
    # Manifest has instance_type but not AMI → gap for AMI
    manifest_with_partial = {
        **_SIMPLE_MANIFEST,
        "parameters": [
            {"parameter": "aws_instance.web.instance_type", "value": "t3.micro",
             "source": "parsed", "reasoning": None},
        ],
    }
    result = lambda_handler(
        _make_event(),
        lambda_context,
        s3_client=_make_s3_mock(ir=ir_with_gap, manifest=manifest_with_partial),
        bedrock_client=MagicMock(),
    )

    body = _body(result)
    assert result["response"]["httpStatusCode"] == 200
    assert body["status"] == "gaps_found"
    assert isinstance(body["gaps"], list)
    assert len(body["gaps"]) > 0


# ---------------------------------------------------------------------------
# Gap resolution exception
# ---------------------------------------------------------------------------


def test_gap_resolution_failure_returns_500(lambda_context, monkeypatch):
    monkeypatch.setattr(_mod, "resolve_gaps", lambda ir, manifest: (_ for _ in ()).throw(RuntimeError("resolver exploded")))
    result = lambda_handler(
        _make_event(),
        lambda_context,
        s3_client=_make_s3_mock(),
        bedrock_client=MagicMock(),
    )

    assert result["response"]["httpStatusCode"] == 500
    assert "Gap resolution" in _body(result)["error"]


# ---------------------------------------------------------------------------
# Environment variable guard
# ---------------------------------------------------------------------------


def test_missing_bedrock_model_id_returns_500(lambda_context, monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID")
    result = lambda_handler(
        _make_event(),
        lambda_context,
        s3_client=_make_s3_mock(),
        bedrock_client=MagicMock(),
    )

    assert result["response"]["httpStatusCode"] == 500
    assert "BEDROCK_MODEL_ID" in _body(result)["error"]


# ---------------------------------------------------------------------------
# Bedrock invocation failure
# ---------------------------------------------------------------------------


def test_bedrock_failure_returns_500(lambda_context):
    bedrock = MagicMock()
    bedrock.invoke_model.side_effect = RuntimeError("ThrottlingException")
    result = lambda_handler(
        _make_event(),
        lambda_context,
        s3_client=_make_s3_mock(),
        bedrock_client=bedrock,
    )

    assert result["response"]["httpStatusCode"] == 500
    assert "Bedrock" in _body(result)["error"]


# ---------------------------------------------------------------------------
# S3 write failure
# ---------------------------------------------------------------------------


def test_s3_write_failure_returns_500(lambda_context):
    s3 = _make_s3_mock()
    s3.put_object.side_effect = RuntimeError("AccessDenied")
    result = lambda_handler(
        _make_event(),
        lambda_context,
        s3_client=s3,
        bedrock_client=_make_bedrock_mock(),
    )

    assert result["response"]["httpStatusCode"] == 500
    assert "S3" in _body(result)["error"]


# ---------------------------------------------------------------------------
# Success path: S3 key derivation and response fields
# ---------------------------------------------------------------------------


def test_success_hcl_key_derived_from_ir_key(lambda_context):
    result = lambda_handler(
        _make_event(),
        lambda_context,
        s3_client=_make_s3_mock(),
        bedrock_client=_make_bedrock_mock(),
    )

    body = _body(result)
    assert body["status"] == "success"
    # IR key: "diagrams/my_arch/ir.json" → HCL key: "diagrams/my_arch/generated.tf"
    assert body["hcl_s3_key"] == "diagrams/my_arch/generated.tf"


def test_success_manifest_key_derived_from_ir_key(lambda_context):
    result = lambda_handler(
        _make_event(),
        lambda_context,
        s3_client=_make_s3_mock(),
        bedrock_client=_make_bedrock_mock(),
    )

    body = _body(result)
    assert body["manifest_s3_key"] == "diagrams/my_arch/manifest_final.json"


def test_success_response_includes_s3_bucket(lambda_context):
    result = lambda_handler(
        _make_event(),
        lambda_context,
        s3_client=_make_s3_mock(),
        bedrock_client=_make_bedrock_mock(),
    )

    assert _body(result)["s3_bucket"] == BUCKET


# ---------------------------------------------------------------------------
# Bedrock response parsing
# ---------------------------------------------------------------------------


def test_nova_model_response_parsed_correctly(lambda_context, monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
    bedrock = _make_bedrock_mock(hcl_text=SAMPLE_HCL, nova=True)
    result = lambda_handler(
        _make_event(),
        lambda_context,
        s3_client=_make_s3_mock(),
        bedrock_client=bedrock,
    )

    assert result["response"]["httpStatusCode"] == 200
    assert _body(result)["status"] == "success"


def test_markdown_fence_stripped_from_hcl_response(lambda_context):
    fenced_hcl = f"```hcl\n{SAMPLE_HCL}\n```"
    bedrock = _make_bedrock_mock(hcl_text=fenced_hcl)

    s3 = _make_s3_mock()
    lambda_handler(
        _make_event(),
        lambda_context,
        s3_client=s3,
        bedrock_client=bedrock,
    )

    # The HCL written to S3 must not contain the fence markers
    hcl_put_call = s3.put_object.call_args_list[0]
    written_body = hcl_put_call.kwargs["Body"].decode("utf-8")
    assert "```" not in written_body
