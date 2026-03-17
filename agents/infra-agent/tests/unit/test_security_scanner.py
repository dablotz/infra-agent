"""Unit tests for security_scanner Lambda handler."""

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
    / "security_scanner"
    / "handler.py"
)
_spec = importlib.util.spec_from_file_location("security_scanner", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

lambda_handler = _mod.lambda_handler
_run_checkov = _mod._run_checkov

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_EVENT = {
    "user_request": "Create an S3 bucket with versioning enabled",
    "iac_type": "terraform",
    "generated_code": 'resource "aws_s3_bucket" "example" {\n  bucket = "my-bucket"\n}',
    "validation_status": "passed",
    "validation_errors": [],
    "retry_count": 0,
}

_CHECKOV_NO_FINDINGS = json.dumps(
    {"results": {"passed_checks": [], "failed_checks": []}}
).encode()

_CHECKOV_WITH_FINDINGS = json.dumps(
    {
        "results": {
            "passed_checks": [],
            "failed_checks": [
                {
                    "check_id": "CKV_AWS_18",
                    "check_name": "Ensure the S3 bucket has access logging enabled",
                    "severity": "MEDIUM",
                    "resource": "aws_s3_bucket.example",
                    "guideline": "https://docs.bridgecrew.io/docs/s3_13",
                }
            ],
        }
    }
).encode()


# ---------------------------------------------------------------------------
# lambda_handler — happy paths
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_no_findings_returns_passed(mock_run, lambda_context):
    mock_run.return_value = MagicMock(stdout=_CHECKOV_NO_FINDINGS, returncode=0)
    result = lambda_handler(BASE_EVENT, lambda_context)

    assert result["security_status"] == "passed"
    assert result["security_findings"] == []


@patch("subprocess.run")
def test_findings_return_warnings_status(mock_run, lambda_context):
    mock_run.return_value = MagicMock(stdout=_CHECKOV_WITH_FINDINGS, returncode=1)
    result = lambda_handler(BASE_EVENT, lambda_context)

    assert result["security_status"] == "warnings"
    assert len(result["security_findings"]) == 1


@patch("subprocess.run")
def test_finding_fields_extracted_correctly(mock_run, lambda_context):
    mock_run.return_value = MagicMock(stdout=_CHECKOV_WITH_FINDINGS, returncode=1)
    result = lambda_handler(BASE_EVENT, lambda_context)

    finding = result["security_findings"][0]
    assert finding["check_id"] == "CKV_AWS_18"
    assert finding["check_name"] == "Ensure the S3 bucket has access logging enabled"
    assert finding["severity"] == "MEDIUM"
    assert finding["resource"] == "aws_s3_bucket.example"
    assert finding["guideline"] == "https://docs.bridgecrew.io/docs/s3_13"


@patch("subprocess.run")
def test_empty_stdout_treated_as_no_findings(mock_run, lambda_context):
    mock_run.return_value = MagicMock(stdout=b"", returncode=0)
    result = lambda_handler(BASE_EVENT, lambda_context)

    assert result["security_status"] == "passed"
    assert result["security_findings"] == []


@patch("subprocess.run")
def test_event_fields_passed_through(mock_run, lambda_context):
    mock_run.return_value = MagicMock(stdout=_CHECKOV_NO_FINDINGS, returncode=0)
    result = lambda_handler(BASE_EVENT, lambda_context)

    assert result["user_request"] == BASE_EVENT["user_request"]
    assert result["validation_status"] == "passed"
    assert result["retry_count"] == 0


# ---------------------------------------------------------------------------
# lambda_handler — infrastructure failures raise (not swallowed)
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_checkov_not_found_raises_runtime_error(mock_run, lambda_context):
    mock_run.side_effect = FileNotFoundError("No such file: python3")
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        lambda_handler(BASE_EVENT, lambda_context)


@patch("subprocess.run")
def test_checkov_timeout_raises_runtime_error(mock_run, lambda_context):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="checkov", timeout=120)
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        lambda_handler(BASE_EVENT, lambda_context)


# ---------------------------------------------------------------------------
# _run_checkov unit tests (internal helper)
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_run_checkov_returns_empty_list_when_no_findings(mock_run):
    mock_run.return_value = MagicMock(stdout=_CHECKOV_NO_FINDINGS, returncode=0)
    findings = _run_checkov("/tmp/test")

    assert findings == []


@patch("subprocess.run")
def test_run_checkov_returns_findings_list(mock_run):
    mock_run.return_value = MagicMock(stdout=_CHECKOV_WITH_FINDINGS, returncode=1)
    findings = _run_checkov("/tmp/test")

    assert len(findings) == 1
    assert findings[0]["check_id"] == "CKV_AWS_18"


@patch("subprocess.run")
def test_run_checkov_handles_invalid_json_gracefully(mock_run):
    mock_run.return_value = MagicMock(stdout=b"not valid json", returncode=1)
    findings = _run_checkov("/tmp/test")

    assert findings == []
