"""Unit tests for iac_agent/terraform_prompt_builder.py and iac_agent/utils.py.

Covers:
  - _build_param_block: prefix filtering, inline comment on agent_default,
    no comment on parsed/user_provided, empty result when no params match.
  - _build_relationship_notes: empty on no rels, formatting, label parens.
  - build_prompt: required-provider/variable instructions, one-resource-per-
    service, unknown-type skipped, AGENT DEFAULT VALUES reminder block,
    relationship notes section.
  - slugify (iac_agent/utils.py): space/hyphen/dot/case handling, fallback
    behaviour, empty inputs.
"""

import importlib.util
import pathlib

import pytest

# ---------------------------------------------------------------------------
# Load modules under test
# ---------------------------------------------------------------------------
_IACAGENT = (
    pathlib.Path(__file__).parent.parent.parent / "lambda_functions" / "iac_agent"
)

_pb_spec = importlib.util.spec_from_file_location(
    "terraform_prompt_builder", _IACAGENT / "terraform_prompt_builder.py"
)
_pb_mod = importlib.util.module_from_spec(_pb_spec)
_pb_spec.loader.exec_module(_pb_mod)

build_prompt = _pb_mod.build_prompt
_build_param_block = _pb_mod._build_param_block
_build_relationship_notes = _pb_mod._build_relationship_notes

_utils_spec = importlib.util.spec_from_file_location(
    "iac_agent_utils", _IACAGENT / "utils.py"
)
_utils_mod = importlib.util.module_from_spec(_utils_spec)
_utils_spec.loader.exec_module(_utils_mod)

slugify = _utils_mod.slugify

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _param(parameter, value, source="parsed", reasoning=None):
    return {"parameter": parameter, "value": value, "source": source, "reasoning": reasoning}


def _ir(services=None, relationships=None):
    return {
        "schema_version": "1.0",
        "source_file": "s3://bucket/diagram.xml",
        "services": services or [],
        "relationships": relationships or [],
        "network": {"vpcs": [], "subnets": [], "security_groups": []},
    }


def _service(service_id, resource_type, label="", config=None):
    return {
        "id": service_id,
        "type": resource_type,
        "label": label or service_id,
        "config": config or {},
    }


def _manifest(parameters=None):
    return {
        "schema_version": "1.0",
        "ir_source": "s3://bucket/ir.json",
        "parameters": parameters or [],
    }


# ===========================================================================
# _build_param_block
# ===========================================================================


def test_param_block_agent_default_includes_inline_comment():
    params = [_param(
        "aws_vpc.main.cidr_block", "10.0.0.0/16",
        source="agent_default", reasoning="RFC-1918 default for dev workloads",
    )]
    result = _build_param_block("aws_vpc", "main", params)

    assert "cidr_block" in result
    assert "# AGENT DEFAULT:" in result
    assert "RFC-1918 default for dev workloads" in result


def test_param_block_parsed_has_no_inline_comment():
    params = [_param("aws_vpc.main.cidr_block", "192.168.0.0/24", source="parsed")]
    result = _build_param_block("aws_vpc", "main", params)

    assert "cidr_block" in result
    assert "# AGENT DEFAULT:" not in result


def test_param_block_user_provided_has_no_inline_comment():
    params = [_param("aws_vpc.main.cidr_block", "172.16.0.0/12", source="user_provided")]
    result = _build_param_block("aws_vpc", "main", params)

    assert "# AGENT DEFAULT:" not in result


def test_param_block_filters_by_prefix():
    """Params belonging to a different resource must not appear."""
    params = [
        _param("aws_vpc.main.cidr_block", "10.0.0.0/16"),
        _param("aws_instance.web.ami", "ami-0abc1234"),
    ]
    result = _build_param_block("aws_vpc", "main", params)

    assert "cidr_block" in result
    assert "ami" not in result


def test_param_block_returns_empty_string_when_no_matching_params():
    params = [_param("aws_instance.web.ami", "ami-0abc1234")]
    result = _build_param_block("aws_vpc", "main", params)

    assert result == ""


def test_param_block_strips_prefix_leaving_only_attribute_name():
    params = [_param("aws_s3_bucket.assets.bucket", "my-bucket", source="parsed")]
    result = _build_param_block("aws_s3_bucket", "assets", params)

    assert 'bucket = "my-bucket"' in result
    # The full parameter path must NOT appear verbatim
    assert "aws_s3_bucket.assets.bucket" not in result


# ===========================================================================
# _build_relationship_notes
# ===========================================================================


def test_relationship_notes_empty_when_no_relationships():
    ir = _ir()
    assert _build_relationship_notes(ir) == ""


def test_relationship_notes_non_empty_when_relationships_exist():
    ir = _ir(relationships=[{
        "source": "web", "target": "db", "relationship_type": "connects_to",
    }])
    result = _build_relationship_notes(ir)

    assert result != ""
    assert "web" in result
    assert "db" in result


def test_relationship_notes_replaces_underscores_with_spaces_in_type():
    ir = _ir(relationships=[{
        "source": "web", "target": "subnet", "relationship_type": "deployed_in",
    }])
    result = _build_relationship_notes(ir)

    assert "deployed in" in result


def test_relationship_notes_includes_label_in_parens():
    ir = _ir(relationships=[{
        "source": "web", "target": "db", "relationship_type": "connects_to",
        "label": "port 5432",
    }])
    result = _build_relationship_notes(ir)

    assert "(port 5432)" in result


def test_relationship_notes_no_parens_when_label_absent():
    ir = _ir(relationships=[{
        "source": "web", "target": "db", "relationship_type": "connects_to",
    }])
    result = _build_relationship_notes(ir)

    assert "()" not in result


# ===========================================================================
# build_prompt
# ===========================================================================


def test_build_prompt_contains_required_providers_instruction():
    prompt = build_prompt(_ir(), _manifest())
    assert "required_providers" in prompt


def test_build_prompt_contains_variable_aws_region():
    prompt = build_prompt(_ir(), _manifest())
    assert 'variable "aws_region"' in prompt


def test_build_prompt_includes_resource_name_for_each_service():
    ir = _ir(services=[
        _service("b1", "aws_s3_bucket", label="uploads"),
        _service("v1", "aws_vpc", label="main vpc"),
    ])
    prompt = build_prompt(ir, _manifest())

    assert "aws_s3_bucket" in prompt
    assert "uploads" in prompt
    assert "aws_vpc" in prompt


def test_build_prompt_skips_service_with_unknown_type():
    ir = _ir(services=[
        _service("u1", "unknown", label="mystery"),
        _service("b1", "aws_s3_bucket", label="store"),
    ])
    prompt = build_prompt(ir, _manifest())

    assert "mystery" not in prompt
    assert "aws_s3_bucket" in prompt


def test_build_prompt_no_resources_placeholder_when_all_services_unknown():
    ir = _ir(services=[_service("u1", "unknown", label="mystery")])
    prompt = build_prompt(ir, _manifest())

    assert "(no resources)" in prompt


def test_build_prompt_empty_services_produces_no_resources():
    prompt = build_prompt(_ir(), _manifest())
    assert "(no resources)" in prompt


def test_build_prompt_agent_defaults_reminder_present_when_defaults_exist():
    ir = _ir(services=[_service("v1", "aws_vpc", label="main")])
    manifest = _manifest(parameters=[
        _param("aws_vpc.main.cidr_block", "10.0.0.0/16",
               source="agent_default", reasoning="dev default"),
    ])
    prompt = build_prompt(ir, manifest)

    assert "AGENT DEFAULT VALUES" in prompt


def test_build_prompt_no_agent_defaults_reminder_when_no_defaults():
    ir = _ir(services=[_service("v1", "aws_vpc", label="main")])
    manifest = _manifest(parameters=[
        _param("aws_vpc.main.cidr_block", "192.168.1.0/24", source="parsed"),
    ])
    prompt = build_prompt(ir, manifest)

    assert "AGENT DEFAULT VALUES" not in prompt


def test_build_prompt_relationship_notes_section_included_when_rels_exist():
    ir = _ir(
        services=[_service("b1", "aws_s3_bucket", label="store")],
        relationships=[{
            "source": "web", "target": "store", "relationship_type": "writes_to",
        }],
    )
    prompt = build_prompt(ir, _manifest())

    assert "writes to" in prompt


def test_build_prompt_slugifies_service_label_for_resource_name():
    """Labels with spaces and hyphens must appear slugified in the prompt."""
    ir = _ir(services=[_service("e1", "aws_instance", label="Web Server")])
    prompt = build_prompt(ir, _manifest())

    assert "web_server" in prompt
    # Raw label with space should not be used as the resource identifier
    assert "Web Server" not in prompt


# ===========================================================================
# slugify (iac_agent/utils.py)
# ===========================================================================


def test_slugify_spaces_to_underscores():
    assert slugify("web server") == "web_server"


def test_slugify_hyphens_to_underscores():
    assert slugify("my-vpc") == "my_vpc"


def test_slugify_dots_to_underscores():
    assert slugify("v1.2.3") == "v1_2_3"


def test_slugify_uppercase_lowercased():
    assert slugify("WebServer") == "webserver"


def test_slugify_mixed_chars_all_converted():
    assert slugify("My VPC-1.0") == "my_vpc_1_0"


def test_slugify_empty_label_uses_fallback():
    assert slugify("", fallback="svc-id") == "svc_id"


def test_slugify_empty_label_and_empty_fallback_returns_empty_string():
    assert slugify("") == ""


def test_slugify_fallback_also_normalised():
    """Fallback strings go through the same normalisation as labels."""
    assert slugify("", fallback="My-Fallback") == "my_fallback"
