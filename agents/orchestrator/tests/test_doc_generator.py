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


# ── Manifest-aware runbook generation ────────────────────────────────────────

MANIFEST_USER_ONLY = {
    "schema_version": "1.0",
    "ir_source": "diagrams/test/ir.json",
    "parameters": [
        {"parameter": "aws_instance.web.instance_type", "value": "t3.medium",
         "source": "user_provided", "reasoning": None},
        {"parameter": "aws_instance.web.ami", "value": "ami-abc123",
         "source": "user_provided", "reasoning": None},
    ],
}

MANIFEST_MIXED = {
    "schema_version": "1.0",
    "ir_source": "diagrams/test/ir.json",
    "parameters": [
        {"parameter": "aws_instance.web.instance_type", "value": "t3.medium",
         "source": "user_provided", "reasoning": None},
        {"parameter": "aws_instance.web.ami", "value": "ami-abc123",
         "source": "parsed", "reasoning": None},
        {"parameter": "aws_instance.web.monitoring", "value": True,
         "source": "agent_default",
         "reasoning": "Enabled by default for production observability."},
    ],
}

MANIFEST_ALL_DEFAULTS = {
    "schema_version": "1.0",
    "ir_source": "diagrams/test/ir.json",
    "parameters": [
        {"parameter": "aws_db_instance.main.multi_az", "value": True,
         "source": "agent_default",
         "reasoning": "Multi-AZ is required for production-grade availability."},
        {"parameter": "aws_db_instance.main.backup_retention_period", "value": 7,
         "source": "agent_default",
         "reasoning": "Seven-day retention is the minimum recommended for production."},
    ],
}

CLAUDE_RUNBOOK_WITH_DEPLOY = (
    "## Infrastructure Overview\n\nCreates a web server.\n\n"
    "## Deployment Steps\n\nRun terraform apply.\n\n"
    "## Rollback Procedure\n\nRun terraform destroy."
)

CLAUDE_RUNBOOK_NO_DEPLOY_HEADER = (
    "## Infrastructure Overview\n\nCreates a web server.\n\n"
    "## Steps\n\nRun terraform apply."
)


def _make_manifest_s3_client(artifact_content=IAC_CONTENT, artifact_metadata=None,
                              manifest_content=None):
    """S3 mock that serves the artifact on first call and the manifest on second."""
    s3 = MagicMock()
    calls = []

    def get_object_side_effect(Bucket, Key):
        calls.append(Key)
        # First call = artifact, second call = manifest (if manifest content provided).
        if len(calls) == 1 or manifest_content is None:
            return {
                "Body": MagicMock(read=lambda: artifact_content.encode("utf-8")),
                "Metadata": artifact_metadata or {"iac-type": "terraform"},
            }
        return {
            "Body": MagicMock(
                read=lambda: json.dumps(manifest_content).encode("utf-8")
            ),
            "Metadata": {},
        }

    s3.get_object.side_effect = get_object_side_effect
    return s3


class TestManifestBackwardsCompat:
    """Lambda must behave identically to the original when no manifest_s3_path is supplied."""

    def test_no_manifest_path_uses_original_prompt(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({"s3_uri": "s3://b/generated/req/file.tf"})
        bedrock = _make_bedrock_client(RUNBOOK_TEXT)
        s3 = _make_s3_client(metadata={"iac-type": "terraform"})

        result = handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        assert result["response"]["httpStatusCode"] == 200
        prompt = json.loads(bedrock.invoke_model.call_args.kwargs["body"])["messages"][0]["content"]
        # Original prompt includes these 7-section headings.
        assert "Resources Created" in prompt
        assert "Teardown" in prompt

    def test_no_manifest_path_doc_written_unchanged(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({"s3_uri": "s3://b/generated/req/file.tf"})
        s3 = _make_s3_client(metadata={"iac-type": "terraform"})
        bedrock = _make_bedrock_client(RUNBOOK_TEXT)

        result = handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        written = s3.put_object.call_args.kwargs["Body"].decode("utf-8")
        assert written == RUNBOOK_TEXT

    def test_manifest_path_ignored_for_non_iac(self, monkeypatch):
        """manifest_s3_path has no effect on non-IaC artifacts."""
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({
            "s3_uri": "s3://b/generated/req/main.go",
            "manifest_s3_path": "s3://b/diagrams/test/manifest.json",
        })
        s3 = _make_s3_client(content=GO_CONTENT, metadata={"code-type": "go"})
        bedrock = _make_bedrock_client(CODE_DOC_TEXT)

        result = handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        assert result["response"]["httpStatusCode"] == 200
        prompt = json.loads(bedrock.invoke_model.call_args.kwargs["body"])["messages"][0]["content"]
        assert "Build" in prompt  # code prompt, not IaC


class TestManifestAllUserProvided:
    def test_configuration_section_injected(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({
            "s3_uri": "s3://b/generated/req/file.tf",
            "manifest_s3_path": "s3://b/diagrams/test/manifest.json",
        })
        s3 = _make_manifest_s3_client(
            artifact_metadata={"iac-type": "terraform"},
            manifest_content=MANIFEST_USER_ONLY,
        )
        bedrock = _make_bedrock_client(CLAUDE_RUNBOOK_WITH_DEPLOY)

        result = handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        assert result["response"]["httpStatusCode"] == 200
        written = s3.put_object.call_args.kwargs["Body"].decode("utf-8")
        assert "## Configuration Decisions" in written
        assert "## Assumptions & Review Items" in written

    def test_section_order(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({
            "s3_uri": "s3://b/generated/req/file.tf",
            "manifest_s3_path": "s3://b/diagrams/test/manifest.json",
        })
        s3 = _make_manifest_s3_client(
            artifact_metadata={"iac-type": "terraform"},
            manifest_content=MANIFEST_USER_ONLY,
        )
        bedrock = _make_bedrock_client(CLAUDE_RUNBOOK_WITH_DEPLOY)

        handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        written = s3.put_object.call_args.kwargs["Body"].decode("utf-8")
        overview_pos = written.index("## Infrastructure Overview")
        config_pos = written.index("## Configuration Decisions")
        deploy_pos = written.index("## Deployment Steps")
        assumptions_pos = written.index("## Assumptions & Review Items")

        assert overview_pos < config_pos < deploy_pos < assumptions_pos

    def test_all_clear_note_in_assumptions(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({
            "s3_uri": "s3://b/generated/req/file.tf",
            "manifest_s3_path": "s3://b/diagrams/test/manifest.json",
        })
        s3 = _make_manifest_s3_client(
            artifact_metadata={"iac-type": "terraform"},
            manifest_content=MANIFEST_USER_ONLY,
        )
        bedrock = _make_bedrock_client(CLAUDE_RUNBOOK_WITH_DEPLOY)

        handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        written = s3.put_object.call_args.kwargs["Body"].decode("utf-8")
        assert "explicitly provided" in written

    def test_manifest_prompt_uses_new_sections(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({
            "s3_uri": "s3://b/generated/req/file.tf",
            "manifest_s3_path": "s3://b/diagrams/test/manifest.json",
        })
        s3 = _make_manifest_s3_client(
            artifact_metadata={"iac-type": "terraform"},
            manifest_content=MANIFEST_USER_ONLY,
        )
        bedrock = _make_bedrock_client(CLAUDE_RUNBOOK_WITH_DEPLOY)

        handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        prompt = json.loads(bedrock.invoke_model.call_args.kwargs["body"])["messages"][0]["content"]
        assert "Infrastructure Overview" in prompt
        assert "Rollback Procedure" in prompt
        # Original-only sections must not be in the manifest prompt.
        assert "Resources Created" not in prompt
        assert "Teardown" not in prompt


class TestManifestMixed:
    def test_production_flag_in_assumptions(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({
            "s3_uri": "s3://b/generated/req/file.tf",
            "manifest_s3_path": "s3://b/diagrams/test/manifest.json",
        })
        s3 = _make_manifest_s3_client(
            artifact_metadata={"iac-type": "terraform"},
            manifest_content=MANIFEST_MIXED,
        )
        bedrock = _make_bedrock_client(CLAUDE_RUNBOOK_WITH_DEPLOY)

        handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        written = s3.put_object.call_args.kwargs["Body"].decode("utf-8")
        assert "monitoring" in written
        assert "⚠ Review before production" in written

    def test_parsed_source_shows_extracted_note(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({
            "s3_uri": "s3://b/generated/req/file.tf",
            "manifest_s3_path": "s3://b/diagrams/test/manifest.json",
        })
        s3 = _make_manifest_s3_client(
            artifact_metadata={"iac-type": "terraform"},
            manifest_content=MANIFEST_MIXED,
        )
        bedrock = _make_bedrock_client(CLAUDE_RUNBOOK_WITH_DEPLOY)

        handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        written = s3.put_object.call_args.kwargs["Body"].decode("utf-8")
        assert "Extracted from diagram" in written


class TestManifestAllAgentDefault:
    def test_multiple_flagged_entries_in_assumptions(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({
            "s3_uri": "s3://b/generated/req/file.tf",
            "manifest_s3_path": "s3://b/diagrams/test/manifest.json",
        })
        s3 = _make_manifest_s3_client(
            artifact_metadata={"iac-type": "terraform"},
            manifest_content=MANIFEST_ALL_DEFAULTS,
        )
        bedrock = _make_bedrock_client(CLAUDE_RUNBOOK_WITH_DEPLOY)

        handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        written = s3.put_object.call_args.kwargs["Body"].decode("utf-8")
        assert "multi_az" in written
        assert "backup_retention_period" in written
        assert written.count("⚠ Review before production") >= 2


class TestManifestReadFailure:
    """Manifest S3 read failure must not crash the lambda — fall back gracefully."""

    def test_manifest_read_failure_returns_200(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({
            "s3_uri": "s3://b/generated/req/file.tf",
            "manifest_s3_path": "s3://b/diagrams/test/manifest.json",
        })
        s3 = MagicMock()
        # First call (artifact) succeeds; second call (manifest) raises.
        call_count = {"n": 0}

        def side_effect(Bucket, Key):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "Body": MagicMock(read=lambda: IAC_CONTENT.encode("utf-8")),
                    "Metadata": {"iac-type": "terraform"},
                }
            raise Exception("NoSuchKey")

        s3.get_object.side_effect = side_effect
        bedrock = _make_bedrock_client(RUNBOOK_TEXT)

        result = handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        assert result["response"]["httpStatusCode"] == 200

    def test_manifest_read_failure_uses_original_runbook(self, monkeypatch):
        monkeypatch.setenv("OUTPUT_BUCKET", "bucket")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "model")

        event = _make_event({
            "s3_uri": "s3://b/generated/req/file.tf",
            "manifest_s3_path": "s3://b/diagrams/test/manifest.json",
        })
        s3 = MagicMock()
        call_count = {"n": 0}

        def side_effect(Bucket, Key):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "Body": MagicMock(read=lambda: IAC_CONTENT.encode("utf-8")),
                    "Metadata": {"iac-type": "terraform"},
                }
            raise Exception("NoSuchKey")

        s3.get_object.side_effect = side_effect
        bedrock = _make_bedrock_client(RUNBOOK_TEXT)

        handler.lambda_handler(event, None, s3_client=s3, bedrock_client=bedrock)

        written = s3.put_object.call_args.kwargs["Body"].decode("utf-8")
        # Falls back to the Claude output unchanged (no manifest sections injected).
        assert written == RUNBOOK_TEXT


class TestAssembleManifestRunbook:
    """Unit tests for the assembly helper directly."""

    def test_config_inserted_before_deployment_steps(self):
        manifest = {
            "parameters": [
                {"parameter": "aws_s3_bucket.b.bucket", "value": "x",
                 "source": "user_provided", "reasoning": None},
            ]
        }
        result = handler._assemble_manifest_runbook(CLAUDE_RUNBOOK_WITH_DEPLOY, manifest)

        config_pos = result.index("## Configuration Decisions")
        deploy_pos = result.index("## Deployment Steps")
        assert config_pos < deploy_pos

    def test_assumptions_appended_at_end(self):
        manifest = {"parameters": []}
        result = handler._assemble_manifest_runbook(CLAUDE_RUNBOOK_WITH_DEPLOY, manifest)

        assumptions_pos = result.index("## Assumptions & Review Items")
        rollback_pos = result.index("## Rollback Procedure")
        assert assumptions_pos > rollback_pos

    def test_fallback_when_no_deployment_steps_header(self):
        """When Claude omits the exact header, sections are appended without crashing."""
        manifest = {"parameters": []}
        result = handler._assemble_manifest_runbook(CLAUDE_RUNBOOK_NO_DEPLOY_HEADER, manifest)

        assert "## Configuration Decisions" in result
        assert "## Assumptions & Review Items" in result
