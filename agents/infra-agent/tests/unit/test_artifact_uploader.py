"""Unit tests for artifact_uploader Lambda handler."""

import json
import importlib.util
import pathlib
from unittest.mock import MagicMock, call

import pytest

# ---------------------------------------------------------------------------
# Load handler
# ---------------------------------------------------------------------------
_path = (
    pathlib.Path(__file__).parent.parent.parent
    / "lambda_functions"
    / "artifact_uploader"
    / "handler.py"
)
_spec = importlib.util.spec_from_file_location("artifact_uploader", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

lambda_handler = _mod.lambda_handler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OUTPUT_BUCKET = "infra-agent-iac-output-123456789012"

BASE_EVENT = {
    "user_request": "Create an S3 bucket with versioning enabled",
    "iac_type": "terraform",
    "generated_code": 'resource "aws_s3_bucket" "example" {\n  bucket = "my-bucket"\n}',
    "validation_status": "passed",
    "validation_errors": [],
    "security_status": "passed",
    "security_findings": [],
    "retry_count": 0,
}


@pytest.fixture
def s3_mock():
    mock = MagicMock()
    mock.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
    return mock


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("OUTPUT_BUCKET", OUTPUT_BUCKET)


# ---------------------------------------------------------------------------
# S3 upload behaviour
# ---------------------------------------------------------------------------


def test_two_s3_objects_uploaded(lambda_context, s3_mock):
    """Expects one code object and one metadata JSON object."""
    lambda_handler(BASE_EVENT, lambda_context, s3_client=s3_mock)

    assert s3_mock.put_object.call_count == 2


def test_code_object_content_and_bucket(lambda_context, s3_mock):
    lambda_handler(BASE_EVENT, lambda_context, s3_client=s3_mock)

    code_call = s3_mock.put_object.call_args_list[0].kwargs
    assert code_call["Bucket"] == OUTPUT_BUCKET
    assert code_call["Body"] == BASE_EVENT["generated_code"].encode("utf-8")
    assert code_call["ContentType"] == "text/plain"


def test_metadata_object_is_valid_json(lambda_context, s3_mock):
    lambda_handler(BASE_EVENT, lambda_context, s3_client=s3_mock)

    meta_call = s3_mock.put_object.call_args_list[1].kwargs
    assert meta_call["ContentType"] == "application/json"
    parsed = json.loads(meta_call["Body"].decode("utf-8"))
    assert parsed["user_request"] == BASE_EVENT["user_request"]


# ---------------------------------------------------------------------------
# S3 key format
# ---------------------------------------------------------------------------


def test_terraform_key_has_tf_extension(lambda_context, s3_mock):
    result = lambda_handler(BASE_EVENT, lambda_context, s3_client=s3_mock)

    assert result["s3_key"].endswith(".tf")


def test_cloudformation_key_has_yaml_extension(lambda_context, s3_mock):
    event = {**BASE_EVENT, "iac_type": "cloudformation"}
    result = lambda_handler(event, lambda_context, s3_client=s3_mock)

    assert result["s3_key"].endswith(".yaml")


def test_key_is_nested_under_generated_prefix(lambda_context, s3_mock):
    result = lambda_handler(BASE_EVENT, lambda_context, s3_client=s3_mock)

    assert result["s3_key"].startswith("generated/")


# ---------------------------------------------------------------------------
# Response fields
# ---------------------------------------------------------------------------


def test_response_contains_required_fields(lambda_context, s3_mock):
    result = lambda_handler(BASE_EVENT, lambda_context, s3_client=s3_mock)

    for field in ("request_id", "s3_bucket", "s3_key", "s3_metadata_key", "s3_uri"):
        assert field in result, f"Missing field: {field}"


def test_s3_bucket_in_response(lambda_context, s3_mock):
    result = lambda_handler(BASE_EVENT, lambda_context, s3_client=s3_mock)

    assert result["s3_bucket"] == OUTPUT_BUCKET


def test_s3_uri_matches_bucket_and_key(lambda_context, s3_mock):
    result = lambda_handler(BASE_EVENT, lambda_context, s3_client=s3_mock)

    assert result["s3_uri"] == f"s3://{OUTPUT_BUCKET}/{result['s3_key']}"


def test_original_event_fields_preserved(lambda_context, s3_mock):
    result = lambda_handler(BASE_EVENT, lambda_context, s3_client=s3_mock)

    assert result["generated_code"] == BASE_EVENT["generated_code"]
    assert result["validation_status"] == "passed"
    assert result["security_status"] == "passed"
    assert result["retry_count"] == 0


# ---------------------------------------------------------------------------
# Object metadata
# ---------------------------------------------------------------------------


def test_s3_object_metadata_set_correctly(lambda_context, s3_mock):
    lambda_handler(BASE_EVENT, lambda_context, s3_client=s3_mock)

    metadata = s3_mock.put_object.call_args_list[0].kwargs["Metadata"]
    assert metadata["iac-type"] == "terraform"
    assert metadata["validation-status"] == "passed"
    assert metadata["security-status"] == "passed"
    assert "request-id" in metadata


def test_user_request_truncated_to_1024_in_metadata(lambda_context, s3_mock):
    event = {**BASE_EVENT, "user_request": "x" * 2000}
    lambda_handler(event, lambda_context, s3_client=s3_mock)

    metadata = s3_mock.put_object.call_args_list[0].kwargs["Metadata"]
    assert len(metadata["user-request"]) == 1024


def test_each_invocation_generates_unique_request_id(lambda_context, s3_mock):
    result_a = lambda_handler(BASE_EVENT, lambda_context, s3_client=s3_mock)
    result_b = lambda_handler(BASE_EVENT, lambda_context, s3_client=s3_mock)

    assert result_a["request_id"] != result_b["request_id"]
