"""Unit tests for validator Lambda handler.

subprocess.run is patched globally so neither terraform nor tflint binaries
need to be present during testing.
"""

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
    "user_request": "Create an S3 bucket with versioning enabled",
    "iac_type": "terraform",
    "generated_code": VALID_TF_CODE,
    "retry_count": 0,
}

# Successful subprocess return value (non-zero is checked only for tflint)
_OK = MagicMock(returncode=0, stdout=b"", stderr=b"")


# ---------------------------------------------------------------------------
# Non-Terraform passthrough
# ---------------------------------------------------------------------------


def test_non_terraform_skips_validation(lambda_context):
    event = {**BASE_EVENT, "iac_type": "cloudformation"}
    result = lambda_handler(event, lambda_context)

    assert result["validation_status"] == "skipped"
    assert result["validation_errors"] == []


def test_cdk_skips_validation(lambda_context):
    event = {**BASE_EVENT, "iac_type": "cdk"}
    result = lambda_handler(event, lambda_context)

    assert result["validation_status"] == "skipped"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_all_checks_pass_returns_passed(mock_run, lambda_context):
    mock_run.return_value = _OK
    result = lambda_handler(BASE_EVENT, lambda_context)

    assert result["validation_status"] == "passed"
    assert result["validation_errors"] == []


@patch("subprocess.run")
def test_event_fields_passed_through_on_success(mock_run, lambda_context):
    mock_run.return_value = _OK
    result = lambda_handler(BASE_EVENT, lambda_context)

    assert result["user_request"] == BASE_EVENT["user_request"]
    assert result["generated_code"] == BASE_EVENT["generated_code"]
    assert result["iac_type"] == "terraform"
    assert result["retry_count"] == 0


# ---------------------------------------------------------------------------
# terraform init failure
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_terraform_init_failure_returns_failed(mock_run, lambda_context):
    mock_run.side_effect = subprocess.CalledProcessError(
        1, "terraform", stderr=b"Error: Could not load plugin"
    )
    result = lambda_handler(BASE_EVENT, lambda_context)

    assert result["validation_status"] == "failed"
    assert any("init failed" in e for e in result["validation_errors"])
    assert any("Could not load plugin" in e for e in result["validation_errors"])


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

    assert result["validation_status"] == "failed"
    assert any("validate failed" in e for e in result["validation_errors"])
    assert any("Missing required argument" in e for e in result["validation_errors"])


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

    assert result["validation_status"] == "failed"
    assert any("tflint" in e for e in result["validation_errors"])
    assert any("Missing version constraints" in e for e in result["validation_errors"])


@patch("subprocess.run")
def test_tflint_clean_does_not_add_errors(mock_run, lambda_context):
    mock_run.side_effect = [
        _OK,  # terraform init
        _OK,  # terraform validate
        _OK,  # tflint --init
        MagicMock(returncode=0, stdout=b"", stderr=b""),  # tflint clean
    ]
    result = lambda_handler(BASE_EVENT, lambda_context)

    assert result["validation_status"] == "passed"
    assert result["validation_errors"] == []


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
