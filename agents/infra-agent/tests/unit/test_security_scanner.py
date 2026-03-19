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
SAMPLE_CODE = 'resource "aws_s3_bucket" "example" {\n  bucket = "my-bucket"\n}'

BASE_EVENT = {
    "actionGroup": "ScanIaC",
    "apiPath": "/scan-iac",
    "httpMethod": "POST",
    "requestBody": {
        "content": {
            "application/json": {
                "properties": [
                    {"name": "generated_code", "type": "string", "value": SAMPLE_CODE},
                    {"name": "iac_type", "type": "string", "value": "terraform"},
                ]
            }
        }
    },
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


def _body(result):
    return json.loads(result["response"]["responseBody"]["application/json"]["body"])


# ---------------------------------------------------------------------------
# Bedrock action group envelope
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_returns_bedrock_envelope(mock_run, lambda_context):
    mock_run.return_value = MagicMock(stdout=_CHECKOV_NO_FINDINGS, returncode=0)
    result = lambda_handler(BASE_EVENT, lambda_context)

    assert result["messageVersion"] == "1.0"
    assert result["response"]["actionGroup"] == "ScanIaC"
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
# lambda_handler — happy paths
# ---------------------------------------------------------------------------


@patch("subprocess.run")
def test_no_findings_returns_passed(mock_run, lambda_context):
    mock_run.return_value = MagicMock(stdout=_CHECKOV_NO_FINDINGS, returncode=0)
    result = lambda_handler(BASE_EVENT, lambda_context)
    body = _body(result)

    assert body["security_status"] == "passed"
    assert body["finding_count"] == 0
    assert body["findings_summary"] == ""


@patch("subprocess.run")
def test_findings_return_warnings_status(mock_run, lambda_context):
    mock_run.return_value = MagicMock(stdout=_CHECKOV_WITH_FINDINGS, returncode=1)
    result = lambda_handler(BASE_EVENT, lambda_context)
    body = _body(result)

    assert body["security_status"] == "warnings"
    assert body["finding_count"] == 1


@patch("subprocess.run")
def test_findings_summary_contains_check_id(mock_run, lambda_context):
    mock_run.return_value = MagicMock(stdout=_CHECKOV_WITH_FINDINGS, returncode=1)
    result = lambda_handler(BASE_EVENT, lambda_context)
    body = _body(result)

    assert "CKV_AWS_18" in body["findings_summary"]
    assert "aws_s3_bucket.example" in body["findings_summary"]


@patch("subprocess.run")
def test_empty_stdout_treated_as_no_findings(mock_run, lambda_context):
    mock_run.return_value = MagicMock(stdout=b"", returncode=0)
    result = lambda_handler(BASE_EVENT, lambda_context)
    body = _body(result)

    assert body["security_status"] == "passed"
    assert body["finding_count"] == 0


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
def test_run_checkov_raises_on_unparseable_output(mock_run):
    mock_run.return_value = MagicMock(stdout=b"not valid json", returncode=1)
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        _run_checkov("/tmp/test")
