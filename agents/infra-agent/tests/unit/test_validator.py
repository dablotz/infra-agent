"""Unit tests for validator Lambda handler.

subprocess.run is patched globally so neither terraform nor tflint binaries
need to be present during testing.
"""

import json
import subprocess
import importlib.util
import pathlib
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load handler
# ---------------------------------------------------------------------------
_path = (
    pathlib.Path(__file__).parent.parent.parent
    / "lambda_functions"
    / "validator"
    / "handler.py"
)
_spec = importlib.util.spec_from_file_location("validator", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

lambda_handler = _mod.lambda_handler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VALID_TF_CODE = """\
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

resource "aws_s3_bucket" "example" {
  bucket = "my-example-bucket"
}
"""

BASE_EVENT = {
    "actionGroup": "ValidateIaC",
    "apiPath": "/validate-iac",
    "httpMethod": "POST",
    "requestBody": {
        "content": {
            "application/json": {
                "properties": [
                    {"name": "generated_code", "type": "string", "value": VALID_TF_CODE},
                    {"name": "iac_type", "type": "string", "value": "terraform"},
                ]
            }
        }
    },
}

# Successful subprocess return value (non-zero is checked only for tflint)
_OK = MagicMock(returncode=0, stdout=b"", stderr=b"")


def _body(result):
    return json.loads(result["response"]["responseBody"]["application/json"]["body"])


# ---------------------------------------------------------------------------
# Bedrock action group envelope
# ---------------------------------------------------------------------------


def test_returns_bedrock_envelope(lambda_context):
    event = {
        **BASE_EVENT,
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": [
                        {"name": "generated_code", "type": "string", "value": VALID_TF_CODE},
                        {"name": "iac_type", "type": "string", "value": "cloudformation"},
                    ]
                }
            }
        },
    }
    result = lambda_handler(event, lambda_context)

    assert result["messageVersion"] == "1.0"
    assert result["response"]["actionGroup"] == "ValidateIaC"
    assert result["response"]["httpStatusCode"] == 200


def test_missing_generated_code_returns_400(lambda_context):
    event = {
        **BASE_EVENT,
        "requestBody": {"content": {"application/json": {"properties": []}}},
    }
    result = lambda_handler(event, lambda_context)

    assert result["response"]["httpStatusCode"] == 400
    assert "error" in _body(result)


# ---------------------------------------------------------------------------
# Non-Terraform passthrough
# ---------------------------------------------------------------------------


def test_non_terraform_skips_validation(lambda_context):
    event = {
        **BASE_EVENT,
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": [
                        {"name": "generated_code", "type": "string", "value": VALID_TF_CODE},
                        {"name": "iac_type", "type": "string", "value": "cloudformation"},
                    ]
                }
            }
        },
    }
    result = lambda_handler(event, lambda_context)
    body = _body(result)

    assert body["validation_status"] == "skipped"
    assert body["validation_errors"] == ""


def test_cdk_skips_validation(lambda_context):
    event = {
        **BASE_EVENT,
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": [
                        {"name": "generated_code", "type": "string", "value": VALID_TF_CODE},
                        {"name": "iac_type", "type": "string", "value": "cdk"},
                    ]
                }
            }
        },
    }
    result = lambda_handler(event, lambda_context)

    assert _body(result)["validation_status"] == "skipped"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_all_checks_pass_returns_passed(mock_run, lambda_context):
    mock_run.return_value = _OK
    result = lambda_handler(BASE_EVENT, lambda_context)
    body = _body(result)

    assert body["validation_status"] == "passed"
    assert body["validation_errors"] == ""


# ---------------------------------------------------------------------------
# terraform init failure
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_terraform_init_failure_returns_failed(mock_run, lambda_context):
    mock_run.side_effect = subprocess.CalledProcessError(
        1, "terraform", stderr=b"Error: Could not load plugin"
    )
    result = lambda_handler(BASE_EVENT, lambda_context)
    body = _body(result)

    assert body["validation_status"] == "failed"
    assert "init failed" in body["validation_errors"]
    assert "Could not load plugin" in body["validation_errors"]


@patch("subprocess.run")
def test_terraform_init_failure_returns_immediately(mock_run, lambda_context):
    """init failure should short-circuit — validate and tflint must not run."""
    mock_run.side_effect = subprocess.CalledProcessError(1, "terraform", stderr=b"err")
    lambda_handler(BASE_EVENT, lambda_context)

    assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# terraform validate failure
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_terraform_validate_failure_returns_failed(mock_run, lambda_context):
    mock_run.side_effect = [
        _OK,  # terraform init
        subprocess.CalledProcessError(
            1, "terraform", stderr=b"Error: Missing required argument"
        ),  # terraform validate
        _OK,  # tflint --init
        MagicMock(returncode=0, stdout=b"", stderr=b""),  # tflint
    ]
    result = lambda_handler(BASE_EVENT, lambda_context)
    body = _body(result)

    assert body["validation_status"] == "failed"
    assert "validate failed" in body["validation_errors"]
    assert "Missing required argument" in body["validation_errors"]


# ---------------------------------------------------------------------------
# tflint warnings
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_tflint_warnings_reported_as_failed(mock_run, lambda_context):
    mock_run.side_effect = [
        _OK,  # terraform init
        _OK,  # terraform validate
        _OK,  # tflint --init
        MagicMock(returncode=1, stdout=b"Warning: Missing version constraints", stderr=b""),
    ]
    result = lambda_handler(BASE_EVENT, lambda_context)
    body = _body(result)

    assert body["validation_status"] == "failed"
    assert "tflint" in body["validation_errors"]
    assert "Missing version constraints" in body["validation_errors"]


@patch("subprocess.run")
def test_tflint_clean_does_not_add_errors(mock_run, lambda_context):
    mock_run.side_effect = [
        _OK,  # terraform init
        _OK,  # terraform validate
        _OK,  # tflint --init
        MagicMock(returncode=0, stdout=b"", stderr=b""),  # tflint clean
    ]
    result = lambda_handler(BASE_EVENT, lambda_context)
    body = _body(result)

    assert body["validation_status"] == "passed"
    assert body["validation_errors"] == ""


# ---------------------------------------------------------------------------
# Infrastructure failures raise RuntimeError
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_terraform_binary_missing_raises_runtime_error(mock_run, lambda_context):
    mock_run.side_effect = FileNotFoundError("No such file: /opt/bin/terraform")
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        lambda_handler(BASE_EVENT, lambda_context)


@patch("subprocess.run")
def test_terraform_init_timeout_raises_runtime_error(mock_run, lambda_context):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="/opt/bin/terraform", timeout=60)
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        lambda_handler(BASE_EVENT, lambda_context)


@patch("subprocess.run")
def test_tflint_binary_missing_raises_runtime_error(mock_run, lambda_context):
    mock_run.side_effect = [
        _OK,  # terraform init
        _OK,  # terraform validate
        FileNotFoundError("No such file: /opt/bin/tflint"),
    ]
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        lambda_handler(BASE_EVENT, lambda_context)


@patch("subprocess.run")
def test_terraform_validate_binary_missing_raises_runtime_error(mock_run, lambda_context):
    mock_run.side_effect = [
        _OK,  # terraform init
        FileNotFoundError("No such file: /opt/bin/terraform"),  # terraform validate
    ]
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        lambda_handler(BASE_EVENT, lambda_context)


@patch("subprocess.run")
def test_terraform_validate_timeout_raises_runtime_error(mock_run, lambda_context):
    mock_run.side_effect = [
        _OK,  # terraform init
        subprocess.TimeoutExpired(cmd="/opt/bin/terraform", timeout=60),  # terraform validate
    ]
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        lambda_handler(BASE_EVENT, lambda_context)


@patch("subprocess.run")
def test_tflint_init_timeout_raises_runtime_error(mock_run, lambda_context):
    mock_run.side_effect = [
        _OK,  # terraform init
        _OK,  # terraform validate
        subprocess.TimeoutExpired(cmd="/opt/bin/tflint", timeout=60),  # tflint --init
    ]
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        lambda_handler(BASE_EVENT, lambda_context)


@patch("subprocess.run")
def test_tflint_init_failure_adds_error_but_does_not_raise(mock_run, lambda_context):
    """tflint --init CalledProcessError adds to errors and marks status failed."""
    mock_run.side_effect = [
        _OK,  # terraform init
        _OK,  # terraform validate
        subprocess.CalledProcessError(1, "tflint", stderr=b"plugin install error"),  # tflint --init
    ]
    result = lambda_handler(BASE_EVENT, lambda_context)
    body = _body(result)

    assert body["validation_status"] == "failed"
    assert "tflint failed" in body["validation_errors"]
