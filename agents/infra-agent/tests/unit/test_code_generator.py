"""Unit tests for code_generator Lambda handler.

Covers both initial generation (no validation_errors) and error-guided
regeneration (validation_errors present), since the two code paths are now
handled by the same Lambda.
"""

import json
import importlib.util
import pathlib
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Load handler
# ---------------------------------------------------------------------------
_path = (
    pathlib.Path(__file__).parent.parent.parent
    / "lambda_functions"
    / "code_generator"
    / "handler.py"
)
_spec = importlib.util.spec_from_file_location("code_generator", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

lambda_handler = _mod.lambda_handler
_build_prompt = _mod._build_prompt

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------
CLAUDE_MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"
NOVA_MODEL_ID = "amazon.nova-lite-v1:0"
SAMPLE_TF_CODE = 'resource "aws_s3_bucket" "example" {\n  bucket = "my-bucket"\n}'
FIXED_TF_CODE = 'resource "aws_s3_bucket" "fixed" {\n  bucket = "fixed-bucket"\n}'

BASE_EVENT = {
    "user_request": "Create an S3 bucket with versioning enabled",
    "iac_type": "terraform",
}

REGEN_EVENT = {
    **BASE_EVENT,
    "generated_code": SAMPLE_TF_CODE,
    "validation_errors": [
        "Terraform validate failed: Error: Missing required argument 'bucket'"
    ],
    "retry_count": 1,
}


def _claude_mock(text: str) -> MagicMock:
    mock = MagicMock()
    mock.invoke_model.return_value = {
        "body": MagicMock(
            read=lambda: json.dumps({"content": [{"text": text}]}).encode()
        )
    }
    return mock


def _nova_mock(text: str) -> MagicMock:
    mock = MagicMock()
    mock.invoke_model.return_value = {
        "body": MagicMock(
            read=lambda: json.dumps(
                {"output": {"message": {"content": [{"text": text}]}}}
            ).encode()
        )
    }
    return mock


@pytest.fixture(autouse=True)
def set_claude_model(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", CLAUDE_MODEL_ID)


# ---------------------------------------------------------------------------
# Response structure
# ---------------------------------------------------------------------------


def test_returns_required_fields(lambda_context):
    result = lambda_handler(BASE_EVENT, lambda_context, bedrock_client=_claude_mock(SAMPLE_TF_CODE))

    assert result["statusCode"] == 200
    assert result["generated_code"] == SAMPLE_TF_CODE
    assert result["user_request"] == BASE_EVENT["user_request"]
    assert result["iac_type"] == "terraform"
    assert result["retry_count"] == 0


# ---------------------------------------------------------------------------
# Claude model
# ---------------------------------------------------------------------------


def test_claude_model_extracts_content_text(lambda_context):
    result = lambda_handler(BASE_EVENT, lambda_context, bedrock_client=_claude_mock(SAMPLE_TF_CODE))

    assert result["generated_code"] == SAMPLE_TF_CODE


def test_claude_request_body_format(lambda_context):
    mock = _claude_mock(SAMPLE_TF_CODE)
    lambda_handler(BASE_EVENT, lambda_context, bedrock_client=mock)

    body = json.loads(mock.invoke_model.call_args.kwargs["body"])
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert body["messages"][0]["role"] == "user"
    assert "inferenceConfig" not in body


def test_claude_prompt_contains_user_request(lambda_context):
    mock = _claude_mock(SAMPLE_TF_CODE)
    lambda_handler(BASE_EVENT, lambda_context, bedrock_client=mock)

    body = json.loads(mock.invoke_model.call_args.kwargs["body"])
    assert BASE_EVENT["user_request"] in body["messages"][0]["content"]


# ---------------------------------------------------------------------------
# Markdown stripping
# ---------------------------------------------------------------------------


def test_strips_hcl_fenced_code_block(lambda_context):
    mock = _claude_mock(f"```hcl\n{SAMPLE_TF_CODE}\n```")
    result = lambda_handler(BASE_EVENT, lambda_context, bedrock_client=mock)

    assert "```" not in result["generated_code"]
    assert result["generated_code"] == SAMPLE_TF_CODE


def test_strips_generic_fenced_code_block(lambda_context):
    mock = _claude_mock(f"```\n{SAMPLE_TF_CODE}\n```")
    result = lambda_handler(BASE_EVENT, lambda_context, bedrock_client=mock)

    assert "```" not in result["generated_code"]


def test_strips_leading_and_trailing_whitespace(lambda_context):
    mock = _claude_mock(f"\n\n{SAMPLE_TF_CODE}\n\n")
    result = lambda_handler(BASE_EVENT, lambda_context, bedrock_client=mock)

    assert result["generated_code"] == SAMPLE_TF_CODE


# ---------------------------------------------------------------------------
# Regeneration mode (validation_errors present)
# ---------------------------------------------------------------------------


def test_regeneration_mode_returns_fixed_code(lambda_context):
    result = lambda_handler(REGEN_EVENT, lambda_context, bedrock_client=_claude_mock(FIXED_TF_CODE))

    assert result["generated_code"] == FIXED_TF_CODE
    assert result["statusCode"] == 200


def test_regeneration_mode_retry_count_passed_through(lambda_context):
    """retry_count is incremented by Step Functions IncrementRetry, not this Lambda."""
    result = lambda_handler(REGEN_EVENT, lambda_context, bedrock_client=_claude_mock(FIXED_TF_CODE))

    assert result["retry_count"] == 1


def test_regeneration_mode_prompt_includes_validation_errors(lambda_context):
    mock = _claude_mock(FIXED_TF_CODE)
    lambda_handler(REGEN_EVENT, lambda_context, bedrock_client=mock)

    body = json.loads(mock.invoke_model.call_args.kwargs["body"])
    prompt = body["messages"][0]["content"]
    assert "Missing required argument 'bucket'" in prompt


def test_regeneration_mode_prompt_includes_previous_code(lambda_context):
    mock = _claude_mock(FIXED_TF_CODE)
    lambda_handler(REGEN_EVENT, lambda_context, bedrock_client=mock)

    body = json.loads(mock.invoke_model.call_args.kwargs["body"])
    prompt = body["messages"][0]["content"]
    assert SAMPLE_TF_CODE in prompt


def test_regeneration_mode_without_previous_code_still_works(lambda_context):
    event = {**REGEN_EVENT}
    del event["generated_code"]
    result = lambda_handler(event, lambda_context, bedrock_client=_claude_mock(FIXED_TF_CODE))

    assert result["statusCode"] == 200


def test_generation_mode_prompt_has_no_error_block(lambda_context):
    """Initial generation must not contain regeneration-specific sections."""
    mock = _claude_mock(SAMPLE_TF_CODE)
    lambda_handler(BASE_EVENT, lambda_context, bedrock_client=mock)

    body = json.loads(mock.invoke_model.call_args.kwargs["body"])
    prompt = body["messages"][0]["content"]
    assert "PREVIOUS ATTEMPT" not in prompt
    assert "VALIDATION ERRORS" not in prompt


def test_generation_mode_initial_retry_count_is_zero(lambda_context):
    result = lambda_handler(BASE_EVENT, lambda_context, bedrock_client=_claude_mock(SAMPLE_TF_CODE))

    assert result["retry_count"] == 0


# ---------------------------------------------------------------------------
# _build_prompt unit tests
# ---------------------------------------------------------------------------


def test_build_prompt_generation_mode_no_previous_block():
    prompt = _build_prompt("terraform", "Create an S3 bucket", [], "")

    assert "PREVIOUS ATTEMPT" not in prompt
    assert "VALIDATION ERRORS" not in prompt
    assert "provider configuration and variables" in prompt


def test_build_prompt_regeneration_mode_has_error_block():
    errors = ["Error: Missing required argument"]
    prompt = _build_prompt("terraform", "Create an S3 bucket", errors, "")

    assert "VALIDATION ERRORS IN PREVIOUS ATTEMPT" in prompt
    assert "Error: Missing required argument" in prompt
    assert "Fix the validation errors" in prompt
    assert "proper version constraints" in prompt


def test_build_prompt_regeneration_with_code_includes_previous_code():
    errors = ["Error: invalid resource type"]
    previous = 'resource "bad_resource" "x" {}'
    prompt = _build_prompt("terraform", "Create an S3 bucket", errors, previous)

    assert "PREVIOUS ATTEMPT CODE" in prompt
    assert previous in prompt


def test_build_prompt_regeneration_without_previous_code_omits_code_section():
    errors = ["Error: something wrong"]
    prompt = _build_prompt("terraform", "Create an S3 bucket", errors, "")

    assert "PREVIOUS ATTEMPT CODE" not in prompt
    assert "VALIDATION ERRORS IN PREVIOUS ATTEMPT" in prompt


def test_build_prompt_user_request_always_present():
    user_request = "Create a VPC with two subnets"
    prompt_gen = _build_prompt("terraform", user_request, [], "")
    prompt_regen = _build_prompt("terraform", user_request, ["some error"], "")

    assert user_request in prompt_gen
    assert user_request in prompt_regen
