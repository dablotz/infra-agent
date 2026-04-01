"""
Unit tests for the PNG pipeline: rekognition_step, bedrock_vision_step, and handler.

All AWS calls are mocked — no real Rekognition, Bedrock, or S3 traffic.

The diagram_parser/ directory is added to sys.path so that the png_pipeline
sub-package and its relative imports resolve the same way they do in Lambda.
"""

import base64
import io
import json
import pathlib
import sys

import pytest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Path setup — mirrors the Lambda runtime's working directory
# ---------------------------------------------------------------------------
_DIAGRAM_PARSER_DIR = (
    pathlib.Path(__file__).parent.parent.parent
    / "lambda_functions"
    / "diagram_parser"
)
if str(_DIAGRAM_PARSER_DIR) not in sys.path:
    sys.path.insert(0, str(_DIAGRAM_PARSER_DIR))

from png_pipeline.rekognition_step import detect_services, _guess_service_hint  # noqa: E402
from png_pipeline.bedrock_vision_step import (  # noqa: E402
    analyze_diagram,
    _build_user_prompt,
    _parse_and_stamp,
)
from png_pipeline.handler import lambda_handler, _build_manifest  # noqa: E402


# ---------------------------------------------------------------------------
# Shared test data & helpers
# ---------------------------------------------------------------------------

_BUCKET = "test-bucket"
_PNG_KEY = "uploads/arch.png"
_JPG_KEY = "uploads/arch.jpg"
_STEM = "arch"
_IR_KEY = f"diagrams/{_STEM}/ir.json"
_MANIFEST_KEY = f"diagrams/{_STEM}/manifest.json"

_FAKE_IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # minimal PNG header


def _s3_event(key: str = _PNG_KEY) -> dict:
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": _BUCKET},
                    "object": {"key": key},
                }
            }
        ]
    }


def _make_rekognition_response(labels: list[dict]) -> dict:
    return {"Labels": labels}


def _make_rekognition_label(
    name: str,
    confidence: float,
    *,
    left: float = 0.1,
    top: float = 0.1,
    width: float = 0.1,
    height: float = 0.1,
) -> dict:
    return {
        "Name": name,
        "Confidence": confidence,
        "Instances": [
            {
                "BoundingBox": {
                    "Left": left,
                    "Top": top,
                    "Width": width,
                    "Height": height,
                },
                "Confidence": confidence,
            }
        ],
    }


def _make_rekognition_label_no_instance(name: str, confidence: float) -> dict:
    return {"Name": name, "Confidence": confidence, "Instances": []}


def _bedrock_response_body(ir_dict: dict) -> dict:
    body_bytes = json.dumps({"content": [{"text": json.dumps(ir_dict)}]}).encode()
    return {"body": io.BytesIO(body_bytes)}


def _minimal_ir(source_file: str = "") -> dict:
    return {
        "schema_version": "1.0",
        "source_file": source_file,
        "services": [
            {
                "id": "svc-1",
                "type": "aws_instance",
                "label": "Web Server",
                "config": {},
            },
            {
                "id": "svc-2",
                "type": "aws_s3_bucket",
                "label": "Assets Bucket",
                "config": {},
            },
        ],
        "relationships": [
            {
                "source": "svc-1",
                "target": "svc-2",
                "relationship_type": "connects_to",
                "label": None,
            }
        ],
        "network": {
            "vpcs": [],
            "subnets": [],
            "security_groups": [],
        },
    }


# ===========================================================================
# rekognition_step tests
# ===========================================================================


class TestDetectServices:
    def _mock_rekognition(self, labels: list[dict]) -> MagicMock:
        client = MagicMock()
        client.detect_labels.return_value = _make_rekognition_response(labels)
        return client

    def test_returns_list_of_detected_labels(self):
        results = detect_services(
            _BUCKET,
            _PNG_KEY,
            rekognition_client=self._mock_rekognition([_make_rekognition_label("Server", 92.5)]),
        )
        assert len(results) == 1
        assert results[0]["rekognition_label"] == "Server"
        assert results[0]["confidence"] == pytest.approx(92.5)

    def test_bounding_box_normalised(self):
        results = detect_services(
            _BUCKET,
            _PNG_KEY,
            rekognition_client=self._mock_rekognition(
                [_make_rekognition_label("Database", 85.0, left=0.2, top=0.3, width=0.15, height=0.12)]
            ),
        )
        bb = results[0]["bounding_box"]
        assert bb["left"] == pytest.approx(0.2)
        assert bb["top"] == pytest.approx(0.3)
        assert bb["width"] == pytest.approx(0.15)
        assert bb["height"] == pytest.approx(0.12)

    def test_filters_below_confidence_threshold(self):
        results = detect_services(
            _BUCKET,
            _PNG_KEY,
            confidence_threshold=70.0,
            rekognition_client=self._mock_rekognition(
                [
                    _make_rekognition_label("Server", 80.0),
                    _make_rekognition_label("LowConf", 50.0),
                ]
            ),
        )
        assert len(results) == 1
        assert results[0]["rekognition_label"] == "Server"

    def test_whole_image_label_has_none_bounding_box(self):
        results = detect_services(
            _BUCKET,
            _PNG_KEY,
            rekognition_client=self._mock_rekognition(
                [_make_rekognition_label_no_instance("Cloud", 95.0)]
            ),
        )
        assert results[0]["bounding_box"] is None

    def test_service_hint_populated_for_known_label(self):
        results = detect_services(
            _BUCKET,
            _PNG_KEY,
            rekognition_client=self._mock_rekognition(
                [_make_rekognition_label("Database", 90.0)]
            ),
        )
        assert results[0]["service_hint"] == "aws_db_instance"

    def test_service_hint_none_for_unknown_label(self):
        results = detect_services(
            _BUCKET,
            _PNG_KEY,
            rekognition_client=self._mock_rekognition(
                [_make_rekognition_label("Unicorn", 88.0)]
            ),
        )
        assert results[0]["service_hint"] is None

    def test_results_sorted_by_descending_confidence(self):
        results = detect_services(
            _BUCKET,
            _PNG_KEY,
            rekognition_client=self._mock_rekognition(
                [
                    _make_rekognition_label("Server", 75.0),
                    _make_rekognition_label("Database", 95.0),
                    _make_rekognition_label("Storage", 85.0),
                ]
            ),
        )
        confidences = [r["confidence"] for r in results]
        assert confidences == sorted(confidences, reverse=True)

    def test_empty_response_returns_empty_list(self):
        results = detect_services(
            _BUCKET, _PNG_KEY, rekognition_client=self._mock_rekognition([])
        )
        assert results == []

    def test_passes_bucket_and_key_to_rekognition(self):
        mock_client = self._mock_rekognition([])
        detect_services(_BUCKET, _PNG_KEY, rekognition_client=mock_client)

        call_kwargs = mock_client.detect_labels.call_args[1]
        assert call_kwargs["Image"]["S3Object"]["Bucket"] == _BUCKET
        assert call_kwargs["Image"]["S3Object"]["Name"] == _PNG_KEY

    def test_multiple_instances_from_single_label_emitted_separately(self):
        label = {
            "Name": "Server",
            "Confidence": 90.0,
            "Instances": [
                {"BoundingBox": {"Left": 0.1, "Top": 0.1, "Width": 0.1, "Height": 0.1}},
                {"BoundingBox": {"Left": 0.5, "Top": 0.5, "Width": 0.1, "Height": 0.1}},
            ],
        }
        results = detect_services(
            _BUCKET,
            _PNG_KEY,
            rekognition_client=self._mock_rekognition([label]),
        )
        assert len(results) == 2
        assert all(r["rekognition_label"] == "Server" for r in results)


class TestGuessServiceHint:
    @pytest.mark.parametrize("label,expected_hint", [
        ("Server", "aws_instance"),
        ("Database", "aws_db_instance"),
        ("Storage Bucket", "aws_s3_bucket"),
        ("Lambda Function", "aws_lambda_function"),
        ("Queue Service", "aws_sqs_queue"),
        ("Load Balancer", "aws_lb"),
        ("Totally Unknown Thing", None),
    ])
    def test_hint_mapping(self, label, expected_hint):
        assert _guess_service_hint(label) == expected_hint


# ===========================================================================
# bedrock_vision_step tests
# ===========================================================================


class TestAnalyzeDiagram:
    def _mock_s3(self, image_bytes: bytes = _FAKE_IMAGE_BYTES) -> MagicMock:
        client = MagicMock()
        client.get_object.return_value = {"Body": io.BytesIO(image_bytes)}
        return client

    def _mock_bedrock(self, ir: dict) -> MagicMock:
        client = MagicMock()
        client.invoke_model.return_value = _bedrock_response_body(ir)
        return client

    def test_returns_valid_ir_structure(self):
        ir = analyze_diagram(
            _BUCKET,
            _PNG_KEY,
            rekognition_context=[],
            bedrock_client=self._mock_bedrock(_minimal_ir()),
            s3_client=self._mock_s3(),
        )
        assert ir["schema_version"] == "1.0"
        assert "services" in ir
        assert "relationships" in ir
        assert "network" in ir

    def test_source_file_stamped_with_s3_key(self):
        ir = analyze_diagram(
            _BUCKET,
            _PNG_KEY,
            rekognition_context=[],
            bedrock_client=self._mock_bedrock(_minimal_ir(source_file="")),
            s3_client=self._mock_s3(),
        )
        assert ir["source_file"] == _PNG_KEY

    def test_network_sub_keys_always_present(self):
        partial = _minimal_ir()
        partial["network"] = {}  # missing sub-keys
        ir = analyze_diagram(
            _BUCKET,
            _PNG_KEY,
            rekognition_context=[],
            bedrock_client=self._mock_bedrock(partial),
            s3_client=self._mock_s3(),
        )
        assert ir["network"]["vpcs"] == []
        assert ir["network"]["subnets"] == []
        assert ir["network"]["security_groups"] == []

    def test_strips_markdown_code_fences(self):
        raw = "```json\n" + json.dumps(_minimal_ir()) + "\n```"
        ir = _parse_and_stamp(raw, _PNG_KEY)
        assert ir["source_file"] == _PNG_KEY
        assert len(ir["services"]) == 2

    def test_raises_on_malformed_json(self):
        with pytest.raises(ValueError, match="malformed JSON"):
            _parse_and_stamp("not-valid-json{{{", _PNG_KEY)

    def test_raises_on_missing_required_keys(self):
        incomplete = {"schema_version": "1.0", "services": []}
        with pytest.raises(ValueError, match="missing required keys"):
            _parse_and_stamp(json.dumps(incomplete), _PNG_KEY)

    def test_jpeg_uses_correct_media_type(self):
        mock_bedrock = self._mock_bedrock(_minimal_ir())
        analyze_diagram(
            _BUCKET,
            _JPG_KEY,
            rekognition_context=[],
            bedrock_client=mock_bedrock,
            s3_client=self._mock_s3(),
        )
        call_body = json.loads(mock_bedrock.invoke_model.call_args[1]["body"])
        assert call_body["messages"][0]["content"][0]["source"]["media_type"] == "image/jpeg"

    def test_png_uses_correct_media_type(self):
        mock_bedrock = self._mock_bedrock(_minimal_ir())
        analyze_diagram(
            _BUCKET,
            _PNG_KEY,
            rekognition_context=[],
            bedrock_client=mock_bedrock,
            s3_client=self._mock_s3(),
        )
        call_body = json.loads(mock_bedrock.invoke_model.call_args[1]["body"])
        assert call_body["messages"][0]["content"][0]["source"]["media_type"] == "image/png"

    def test_rekognition_context_included_in_prompt(self):
        context = [
            {
                "rekognition_label": "Server",
                "confidence": 90.0,
                "bounding_box": {"left": 0.1, "top": 0.2, "width": 0.1, "height": 0.1},
                "service_hint": "aws_instance",
            }
        ]
        mock_bedrock = self._mock_bedrock(_minimal_ir())
        analyze_diagram(
            _BUCKET,
            _PNG_KEY,
            rekognition_context=context,
            bedrock_client=mock_bedrock,
            s3_client=self._mock_s3(),
        )
        call_body = json.loads(mock_bedrock.invoke_model.call_args[1]["body"])
        user_text = call_body["messages"][0]["content"][1]["text"]
        assert "Server" in user_text
        assert "90.0" in user_text

    def test_empty_rekognition_context_omits_grounding_section(self):
        prompt = _build_user_prompt([])
        assert "Rekognition" not in prompt

    def test_grounding_section_present_when_context_non_empty(self):
        context = [
            {
                "rekognition_label": "Server",
                "confidence": 88.0,
                "bounding_box": {"left": 0.1, "top": 0.1, "width": 0.1, "height": 0.1},
                "service_hint": None,
            }
        ]
        prompt = _build_user_prompt(context)
        assert "Rekognition Grounding Context" in prompt
        assert "Server" in prompt

    def test_image_base64_encoded_in_request(self):
        mock_bedrock = self._mock_bedrock(_minimal_ir())
        analyze_diagram(
            _BUCKET,
            _PNG_KEY,
            rekognition_context=[],
            bedrock_client=mock_bedrock,
            s3_client=self._mock_s3(_FAKE_IMAGE_BYTES),
        )
        call_body = json.loads(mock_bedrock.invoke_model.call_args[1]["body"])
        encoded = call_body["messages"][0]["content"][0]["source"]["data"]
        assert base64.standard_b64decode(encoded) == _FAKE_IMAGE_BYTES


# ===========================================================================
# handler (orchestrator) tests
# ===========================================================================


class TestPngHandler:
    def _s3(self, image_bytes: bytes = _FAKE_IMAGE_BYTES) -> MagicMock:
        client = MagicMock()
        client.get_object.return_value = {"Body": io.BytesIO(image_bytes)}
        return client

    def _rekognition(self, labels: list[dict]) -> MagicMock:
        client = MagicMock()
        client.detect_labels.return_value = _make_rekognition_response(labels)
        return client

    def _bedrock(self, ir: dict) -> MagicMock:
        client = MagicMock()
        client.invoke_model.return_value = _bedrock_response_body(ir)
        return client

    def test_returns_ir_and_manifest_keys(self):
        result = lambda_handler(
            _s3_event(),
            None,
            s3_client=self._s3(),
            rekognition_client=self._rekognition([]),
            bedrock_client=self._bedrock(_minimal_ir()),
        )
        assert result["ir_s3_key"] == _IR_KEY
        assert result["manifest_s3_key"] == _MANIFEST_KEY
        assert result["service_count"] == 2

    def test_s3_key_derived_from_stem(self):
        result = lambda_handler(
            _s3_event("uploads/my-diagram.png"),
            None,
            s3_client=self._s3(),
            rekognition_client=self._rekognition([]),
            bedrock_client=self._bedrock(_minimal_ir()),
        )
        assert result["ir_s3_key"] == "diagrams/my-diagram/ir.json"
        assert result["manifest_s3_key"] == "diagrams/my-diagram/manifest.json"

    def test_writes_ir_and_manifest_to_s3(self):
        s3 = self._s3()
        lambda_handler(
            _s3_event(),
            None,
            s3_client=s3,
            rekognition_client=self._rekognition([]),
            bedrock_client=self._bedrock(_minimal_ir()),
        )
        written_keys = [c[1]["Key"] for c in s3.put_object.call_args_list]
        assert _IR_KEY in written_keys
        assert _MANIFEST_KEY in written_keys

    def test_ir_written_has_correct_structure(self):
        s3 = self._s3()
        lambda_handler(
            _s3_event(),
            None,
            s3_client=s3,
            rekognition_client=self._rekognition([]),
            bedrock_client=self._bedrock(_minimal_ir()),
        )
        ir_call = next(c for c in s3.put_object.call_args_list if c[1]["Key"] == _IR_KEY)
        ir = json.loads(ir_call[1]["Body"].decode())

        assert ir["schema_version"] == "1.0"
        assert ir["source_file"] == _PNG_KEY
        assert isinstance(ir["services"], list)
        assert isinstance(ir["relationships"], list)
        assert "vpcs" in ir["network"]

    def test_manifest_all_parameters_source_parsed_reasoning_null(self):
        s3 = self._s3()
        lambda_handler(
            _s3_event(),
            None,
            s3_client=s3,
            rekognition_client=self._rekognition([]),
            bedrock_client=self._bedrock(_minimal_ir()),
        )
        manifest_call = next(
            c for c in s3.put_object.call_args_list if c[1]["Key"] == _MANIFEST_KEY
        )
        manifest = json.loads(manifest_call[1]["Body"].decode())

        assert manifest["schema_version"] == "1.0"
        assert manifest["ir_source"] == _PNG_KEY
        assert all(p["source"] == "parsed" for p in manifest["parameters"])
        assert all(p["reasoning"] is None for p in manifest["parameters"])

    def test_manifest_includes_diagram_id_per_service(self):
        s3 = self._s3()
        lambda_handler(
            _s3_event(),
            None,
            s3_client=s3,
            rekognition_client=self._rekognition([]),
            bedrock_client=self._bedrock(_minimal_ir()),
        )
        manifest_call = next(
            c for c in s3.put_object.call_args_list if c[1]["Key"] == _MANIFEST_KEY
        )
        manifest = json.loads(manifest_call[1]["Body"].decode())

        diagram_ids = [p for p in manifest["parameters"] if p["parameter"].endswith(".diagram_id")]
        assert len(diagram_ids) == 2  # one per service in _minimal_ir

    def test_falls_back_to_bedrock_only_when_no_bounding_boxes(self):
        """Whole-image Rekognition labels (no bounding box) should not appear in the prompt."""
        mock_bedrock = self._bedrock(_minimal_ir())
        lambda_handler(
            _s3_event(),
            None,
            s3_client=self._s3(),
            rekognition_client=self._rekognition(
                [_make_rekognition_label_no_instance("Technology", 90.0)]
            ),
            bedrock_client=mock_bedrock,
        )
        call_body = json.loads(mock_bedrock.invoke_model.call_args[1]["body"])
        user_text = call_body["messages"][0]["content"][1]["text"]
        assert "Rekognition Grounding Context" not in user_text

    def test_rekognition_context_forwarded_when_bounded_instances_present(self):
        mock_bedrock = self._bedrock(_minimal_ir())
        lambda_handler(
            _s3_event(),
            None,
            s3_client=self._s3(),
            rekognition_client=self._rekognition(
                [_make_rekognition_label("Server", 92.0)]
            ),
            bedrock_client=mock_bedrock,
        )
        call_body = json.loads(mock_bedrock.invoke_model.call_args[1]["body"])
        user_text = call_body["messages"][0]["content"][1]["text"]
        assert "Rekognition Grounding Context" in user_text
        assert "Server" in user_text

    def test_rekognition_failure_falls_back_gracefully(self):
        """Rekognition exceptions should not abort the pipeline."""
        broken = MagicMock()
        broken.detect_labels.side_effect = Exception("Rekognition unavailable")
        mock_bedrock = self._bedrock(_minimal_ir())

        result = lambda_handler(
            _s3_event(),
            None,
            s3_client=self._s3(),
            rekognition_client=broken,
            bedrock_client=mock_bedrock,
        )
        assert result["service_count"] == 2
        assert "error" not in result

    def test_empty_services_returns_error_payload(self):
        empty_ir = _minimal_ir()
        empty_ir["services"] = []
        empty_ir["relationships"] = []

        result = lambda_handler(
            _s3_event(),
            None,
            s3_client=self._s3(),
            rekognition_client=self._rekognition([]),
            bedrock_client=self._bedrock(empty_ir),
        )
        assert result["error"] is True
        assert result["service_count"] == 0
        assert result["ir_s3_key"] is None

    def test_bedrock_malformed_json_returns_error_payload(self):
        broken_bedrock = MagicMock()
        bad_body = json.dumps({"content": [{"text": "not-json{{{"}]}).encode()
        broken_bedrock.invoke_model.return_value = {"body": io.BytesIO(bad_body)}

        result = lambda_handler(
            _s3_event(),
            None,
            s3_client=self._s3(),
            rekognition_client=self._rekognition([]),
            bedrock_client=broken_bedrock,
        )
        assert result["error"] is True
        assert "malformed JSON" in result["message"]

    def test_url_encoded_s3_key_decoded(self):
        result = lambda_handler(
            _s3_event("uploads/my+arch+diagram.png"),
            None,
            s3_client=self._s3(),
            rekognition_client=self._rekognition([]),
            bedrock_client=self._bedrock(_minimal_ir()),
        )
        assert result["ir_s3_key"] == "diagrams/my arch diagram/ir.json"

    def test_jpg_extension_triggers_jpeg_media_type(self):
        mock_bedrock = self._bedrock(_minimal_ir())
        lambda_handler(
            _s3_event(_JPG_KEY),
            None,
            s3_client=self._s3(),
            rekognition_client=self._rekognition([]),
            bedrock_client=mock_bedrock,
        )
        call_body = json.loads(mock_bedrock.invoke_model.call_args[1]["body"])
        media_type = call_body["messages"][0]["content"][0]["source"]["media_type"]
        assert media_type == "image/jpeg"


# ===========================================================================
# _build_manifest helper tests
# ===========================================================================


class TestBuildManifest:
    def test_schema_version_is_1_0(self):
        manifest = _build_manifest([], ir_source=_PNG_KEY)
        assert manifest["schema_version"] == "1.0"

    def test_ir_source_set_correctly(self):
        manifest = _build_manifest([], ir_source=_PNG_KEY)
        assert manifest["ir_source"] == _PNG_KEY

    def test_all_parameters_source_parsed_reasoning_null(self):
        services = [{"id": "svc-1", "type": "aws_instance", "label": "Web", "config": {}}]
        manifest = _build_manifest(services, ir_source=_PNG_KEY)
        for param in manifest["parameters"]:
            assert param["source"] == "parsed"
            assert param["reasoning"] is None

    def test_config_values_emitted_as_separate_parameters(self):
        services = [
            {
                "id": "svc-1",
                "type": "aws_vpc",
                "label": "Main VPC",
                "config": {"cidr_block": "10.0.0.0/16"},
            }
        ]
        manifest = _build_manifest(services, ir_source=_PNG_KEY)
        params = {p["parameter"]: p["value"] for p in manifest["parameters"]}
        assert "aws_vpc.main_vpc.cidr_block" in params
        assert params["aws_vpc.main_vpc.cidr_block"] == "10.0.0.0/16"
