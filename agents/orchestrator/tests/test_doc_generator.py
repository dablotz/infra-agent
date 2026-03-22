import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "lambda_functions", "doc_generator"
    ),
)

import handler


def _make_event(props):
    return {
        "actionGroup": "GenerateDocs",
        "apiPath": "/generate-docs",
        "httpMethod": "POST",
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": [
                        {"name": k, "value": v} for k, v in props.items()
                    ]
                }
            }
        },
    }


IAC_CONTENT = 'resource "aws_s3_bucket" "example" { bucket = "my-bucket" }'
GO_CONTENT = "package main\n\nimport \"fmt\"\n\nfunc main() { fmt.Println(\"hello\") }"
RUNBOOK_TEXT = "# Runbook\n\n## Overview\nThis creates an S3 bucket."
CODE_DOC_TEXT = "# Documentation\n\n## Overview\nThis is a Go program."


def _make_s3_client(content=IAC_CONTENT, metadata=None):
    s3 = MagicMock()
    s3.get_object.return_value = {
        "Body": MagicMock(read=lambda: content.encode("utf-8")),
        "Metadata": metadata or {},
    }
    return s3


def _make_bedrock_client(doc_text=RUNBOOK_TEXT):
    bedrock = MagicMock()
    bedrock.invoke_model.return_value = {
        "body": MagicMock(
            read=lambda: json.dumps(
                {"content": [{"text": doc_text}]}
            ).encode("utf-8")
        )
    }
    return bedrock


class TestDocGenerator:
    def test_missing_s3_uri_returns_400(self):
        event = _make_event({})
        result = handler.lambda_handler(
            event, None,
            s3_client=_make_s3_client(),
            bedrock_client=_make_bedrock_client(),
        )
        assert result["response"]["httpStatusCode"] == 400
        body = json.loads(result["response"]["responseBody"]["application/json"]["body"])
        assert "error" in body

    def test_invalid_s3_uri_returns_400(self):
        event = _make_event({"s3_uri": "not-a-valid-uri"})
        result = handler.lambda_handler(
            event, None,
            s3_client=_make_s3_client(),
            bedrock_client=_make_bedrock_client(),
        )
        assert result["response"]["httpStatusCode"] == 400

    def test_s3_read_failure_returns_400(self):
        event = _make_event({"s3_uri": "s3://bucket/generated/abc/file.tf"})
        s3 = MagicMock()
        s3.get_object.side_effect = Exception("Access denied")
        result = handler.lambda_handler(
            event, None,
            s3_client=s3,
            bedrock_client=_make_bedrock_client(),
        )
        assert result["response"]["httpStatusCode"] == 400
        body = json.loads(result["response"]["responseBody"]["application/json"]["body"])
        assert "Failed to read artifact from S3" in body["error"]

    def test_successful_generation(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "test-output-bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-test")

        s3_uri = "s3://source-bucket/generated/req-abc-123/20260101-120000.tf"
        event = _make_event({"s3_uri": s3_uri, "artifact_type": "terraform"})

        s3 = _make_s3_client(metadata={"iac-type": "terraform"})
        bedrock = _make_bedrock_client()

        result = handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        assert result["response"]["httpStatusCode"] == 200
        body = json.loads(result["response"]["responseBody"]["application/json"]["body"])

        assert "doc_s3_uri" in body
        assert body["doc_s3_uri"].startswith("s3://test-output-bucket/docs/")
        assert body["request_id"] == "req-abc-123"

    def test_request_id_extracted_from_key(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "my-bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-test")

        s3_uri = "s3://bucket/generated/my-request-id/20260321-100000.yaml"
        event = _make_event({"s3_uri": s3_uri, "artifact_type": "cloudformation"})

        result = handler.lambda_handler(
            event, None,
            s3_client=_make_s3_client(metadata={"iac-type": "cloudformation"}),
            bedrock_client=_make_bedrock_client(),
        )

        body = json.loads(result["response"]["responseBody"]["application/json"]["body"])
        assert body["request_id"] == "my-request-id"

    def test_doc_written_to_s3(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "test-output-bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-test")

        s3_uri = "s3://bucket/generated/req-123/20260101-120000.tf"
        event = _make_event({"s3_uri": s3_uri})

        s3 = _make_s3_client()
        result = handler.lambda_handler(
            event, None,
            s3_client=s3,
            bedrock_client=_make_bedrock_client(),
        )

        assert result["response"]["httpStatusCode"] == 200
        s3.put_object.assert_called_once()
        call_kwargs = s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "test-output-bucket"
        assert call_kwargs["Key"].startswith("docs/req-123/")
        assert call_kwargs["Key"].endswith(".md")
        assert call_kwargs["ContentType"] == "text/markdown"

    def test_response_envelope(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "test-output-bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-test")

        event = _make_event({"s3_uri": "s3://bucket/generated/req/20260101.tf"})
        event["actionGroup"] = "GenerateDocs"
        event["apiPath"] = "/generate-docs"

        result = handler.lambda_handler(
            event, None,
            s3_client=_make_s3_client(),
            bedrock_client=_make_bedrock_client(),
        )

        assert result["messageVersion"] == "1.0"
        assert result["response"]["actionGroup"] == "GenerateDocs"
        assert result["response"]["apiPath"] == "/generate-docs"

    # ── Artifact type resolution ──────────────────────────────────────────────

    def test_artifact_type_read_from_s3_metadata_iac_type_key(self, monkeypatch):
        """S3 metadata 'iac-type' key takes precedence over caller-supplied hint."""
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({
            "s3_uri": "s3://b/generated/r/f.tf",
            "artifact_type": "cloudformation",  # caller hint
        })
        bedrock = _make_bedrock_client()
        # S3 metadata says terraform — should win
        s3 = _make_s3_client(metadata={"iac-type": "terraform"})
        handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        prompt = json.loads(bedrock.invoke_model.call_args.kwargs["body"])["messages"][0]["content"]
        assert "terraform" in prompt
        assert "Deployment Steps" in prompt  # IaC-style section

    def test_artifact_type_read_from_s3_metadata_code_type_key(self, monkeypatch):
        """S3 metadata 'code-type' key is used for non-IaC agents."""
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({"s3_uri": "s3://b/generated/r/main.go"})
        bedrock = _make_bedrock_client(CODE_DOC_TEXT)
        s3 = _make_s3_client(content=GO_CONTENT, metadata={"code-type": "go"})
        handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        prompt = json.loads(bedrock.invoke_model.call_args.kwargs["body"])["messages"][0]["content"]
        assert "go" in prompt
        assert "Build" in prompt  # code-style section
        assert "Teardown" not in prompt  # IaC section absent

    def test_artifact_type_fallback_to_caller_hint(self, monkeypatch):
        """Falls back to caller-supplied artifact_type when S3 metadata has no type key."""
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({"s3_uri": "s3://b/generated/r/main.rs", "artifact_type": "rust"})
        bedrock = _make_bedrock_client(CODE_DOC_TEXT)
        s3 = _make_s3_client(content="fn main() {}", metadata={})
        handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        prompt = json.loads(bedrock.invoke_model.call_args.kwargs["body"])["messages"][0]["content"]
        assert "rust" in prompt
        assert "Build" in prompt

    # ── Prompt template dispatch ──────────────────────────────────────────────

    def test_iac_prompt_contains_runbook_sections(self):
        for iac_type in ("terraform", "cloudformation", "cdk"):
            prompt = handler._build_prompt(IAC_CONTENT, iac_type)
            assert "Deployment Steps" in prompt
            assert "Resources Created" in prompt
            assert "Teardown" in prompt
            assert "Build" not in prompt

    def test_code_prompt_contains_developer_sections(self):
        for lang in ("go", "rust", "python", "java"):
            prompt = handler._build_prompt(GO_CONTENT, lang)
            assert "Build" in prompt
            assert "Test" in prompt
            assert "Dependencies" in prompt
            assert "Teardown" not in prompt
            assert "Resources Created" not in prompt
