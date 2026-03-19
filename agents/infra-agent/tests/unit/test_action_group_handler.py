"""Unit tests for action_group_handler.

Loads the handler via importlib under a unique module name to avoid
sys.modules collisions with other handler.py files in the same pytest session.
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
    / "action_group_handler"
    / "handler.py"
)
_spec = importlib.util.spec_from_file_location("action_group_handler", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

lambda_handler = _mod.lambda_handler
MAX_REQUEST_LENGTH = _mod.MAX_REQUEST_LENGTH
VALID_IAC_TYPES = _mod.VALID_IAC_TYPES

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------
STATE_MACHINE_ARN = (
    "arn:aws:states:us-east-1:123456789012:stateMachine:infra-agent-iac-generator"
)
EXECUTION_ARN = (
    "arn:aws:states:us-east-1:123456789012:execution:infra-agent-iac-generator:abc-123"
)
PIPELINE_OUTPUT = {
    "s3_uri": "s3://infra-agent-iac-output-123456789012/generated/abc/20260101-000000.tf",
    "s3_bucket": "infra-agent-iac-output-123456789012",
    "s3_key": "generated/abc/20260101-000000.tf",
    "iac_type": "terraform",
    "validation_status": "passed",
    "security_status": "passed",
}

# Bedrock sends requestBody parameters under this path
BASE_EVENT = {
    "actionGroup": "IaCGeneratorActionGroup",
    "apiPath": "/generate-iac",
    "httpMethod": "POST",
    "requestBody": {
        "content": {
            "application/json": {
                "properties": [
                    {
                        "name": "user_request",
                        "type": "string",
                        "value": "Create an S3 bucket with versioning enabled",
                    },
                    {"name": "iac_type", "type": "string", "value": "terraform"},
                ]
            }
        }
    },
}


def _make_sfn_mock(status="SUCCEEDED", output=None):
    mock = MagicMock()
    mock.start_execution.return_value = {"executionArn": EXECUTION_ARN}
    mock.describe_execution.return_value = {
        "status": status,
        "output": json.dumps(output or PIPELINE_OUTPUT),
    }
    return mock


@pytest.fixture
def sfn_mock():
    return _make_sfn_mock()


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("STATE_MACHINE_ARN", STATE_MACHINE_ARN)


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_valid_request_starts_sfn_execution(lambda_context, sfn_mock):
    lambda_handler(BASE_EVENT, lambda_context, sfn_client=sfn_mock)

    sfn_mock.start_execution.assert_called_once()
    call_kwargs = sfn_mock.start_execution.call_args.kwargs
    assert call_kwargs["stateMachineArn"] == STATE_MACHINE_ARN
    body = json.loads(call_kwargs["input"])
    assert body["user_request"] == "Create an S3 bucket with versioning enabled"
    assert body["iac_type"] == "terraform"
    assert body["retry_count"] == 0


def test_valid_request_returns_200(lambda_context, sfn_mock):
    result = lambda_handler(BASE_EVENT, lambda_context, sfn_client=sfn_mock)

    assert result["messageVersion"] == "1.0"
    response = result["response"]
    assert response["httpStatusCode"] == 200
    body = json.loads(response["responseBody"]["application/json"]["body"])
    assert body["message"] == "IaC generated successfully"
    assert body["s3_uri"] == PIPELINE_OUTPUT["s3_uri"]
    assert body["validation_status"] == "passed"
    assert body["security_status"] == "passed"


def test_action_group_and_api_path_echoed(lambda_context, sfn_mock):
    result = lambda_handler(BASE_EVENT, lambda_context, sfn_client=sfn_mock)

    response = result["response"]
    assert response["actionGroup"] == "IaCGeneratorActionGroup"
    assert response["apiPath"] == "/generate-iac"


def test_defaults_iac_type_to_terraform(lambda_context, sfn_mock):
    event = {
        **BASE_EVENT,
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": [
                        {
                            "name": "user_request",
                            "type": "string",
                            "value": "Create an S3 bucket",
                        }
                    ]
                }
            }
        },
    }
    lambda_handler(event, lambda_context, sfn_client=sfn_mock)

    body = json.loads(sfn_mock.start_execution.call_args.kwargs["input"])
    assert body["iac_type"] == "terraform"


def test_falls_back_to_parameters_field(lambda_context, sfn_mock):
    """Legacy/fallback: parameters at top level (path/query params) still work."""
    event = {
        "actionGroup": "IaCGeneratorActionGroup",
        "apiPath": "/generate-iac",
        "httpMethod": "POST",
        "parameters": [
            {"name": "user_request", "type": "string", "value": "Create an S3 bucket"},
            {"name": "iac_type", "type": "string", "value": "terraform"},
        ],
    }
    result = lambda_handler(event, lambda_context, sfn_client=sfn_mock)
    assert result["response"]["httpStatusCode"] == 200


# ---------------------------------------------------------------------------
# Pipeline failure tests
# ---------------------------------------------------------------------------


def test_pipeline_failure_returns_500(lambda_context):
    sfn_mock = _make_sfn_mock(status="FAILED")
    sfn_mock.describe_execution.return_value = {
        "status": "FAILED",
        "cause": "Validation error",
    }
    result = lambda_handler(BASE_EVENT, lambda_context, sfn_client=sfn_mock)

    assert result["response"]["httpStatusCode"] == 500
    body = json.loads(result["response"]["responseBody"]["application/json"]["body"])
    assert "failed" in body["error"]


# ---------------------------------------------------------------------------
# Input validation tests
# ---------------------------------------------------------------------------


def test_missing_user_request_returns_400(lambda_context, sfn_mock):
    event = {
        **BASE_EVENT,
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": [
                        {"name": "iac_type", "type": "string", "value": "terraform"}
                    ]
                }
            }
        },
    }
    result = lambda_handler(event, lambda_context, sfn_client=sfn_mock)

    assert result["response"]["httpStatusCode"] == 400
    body = json.loads(result["response"]["responseBody"]["application/json"]["body"])
    assert "error" in body
    sfn_mock.start_execution.assert_not_called()


def test_empty_user_request_returns_400(lambda_context, sfn_mock):
    event = {
        **BASE_EVENT,
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": [
                        {"name": "user_request", "type": "string", "value": ""},
                    ]
                }
            }
        },
    }
    result = lambda_handler(event, lambda_context, sfn_client=sfn_mock)

    assert result["response"]["httpStatusCode"] == 400
    sfn_mock.start_execution.assert_not_called()


def test_oversized_user_request_returns_400(lambda_context, sfn_mock):
    event = {
        **BASE_EVENT,
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": [
                        {
                            "name": "user_request",
                            "type": "string",
                            "value": "x" * (MAX_REQUEST_LENGTH + 1),
                        }
                    ]
                }
            }
        },
    }
    result = lambda_handler(event, lambda_context, sfn_client=sfn_mock)

    assert result["response"]["httpStatusCode"] == 400
    sfn_mock.start_execution.assert_not_called()


def test_invalid_iac_type_returns_400(lambda_context, sfn_mock):
    event = {
        **BASE_EVENT,
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": [
                        {"name": "user_request", "type": "string", "value": "Create an S3 bucket"},
                        {"name": "iac_type", "type": "string", "value": "pulumi"},
                    ]
                }
            }
        },
    }
    result = lambda_handler(event, lambda_context, sfn_client=sfn_mock)

    assert result["response"]["httpStatusCode"] == 400
    body = json.loads(result["response"]["responseBody"]["application/json"]["body"])
    assert "iac_type" in body["error"]
    sfn_mock.start_execution.assert_not_called()


def test_max_length_user_request_is_accepted(lambda_context, sfn_mock):
    event = {
        **BASE_EVENT,
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": [
                        {
                            "name": "user_request",
                            "type": "string",
                            "value": "x" * MAX_REQUEST_LENGTH,
                        }
                    ]
                }
            }
        },
    }
    result = lambda_handler(event, lambda_context, sfn_client=sfn_mock)

    assert result["response"]["httpStatusCode"] == 200
    sfn_mock.start_execution.assert_called_once()
