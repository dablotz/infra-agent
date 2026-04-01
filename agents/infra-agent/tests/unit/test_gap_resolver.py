"""Unit tests for iac_agent/gap_resolver.py.

Covers three scenarios:
  1. All required values already present — no defaults applied, no gaps.
  2. Some parameters missing but safely defaultable — defaults applied.
  3. Parameters that cannot be safely defaulted — returned as gaps.
"""

import importlib.util
import pathlib

import pytest

# ---------------------------------------------------------------------------
# Load the module under test
# ---------------------------------------------------------------------------
_path = (
    pathlib.Path(__file__).parent.parent.parent
    / "lambda_functions"
    / "iac_agent"
    / "gap_resolver.py"
)
_spec = importlib.util.spec_from_file_location("gap_resolver", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

resolve_gaps = _mod.resolve_gaps

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _ir(*services):
    return {
        "schema_version": "1.0",
        "source_file": "s3://bucket/diagram.xml",
        "services": list(services),
        "relationships": [],
        "network": {"vpcs": [], "subnets": [], "security_groups": []},
    }


def _service(service_id: str, resource_type: str, label: str = "", config: dict | None = None):
    return {
        "id": service_id,
        "type": resource_type,
        "label": label or service_id,
        "config": config or {},
    }


def _manifest(ir_source: str = "s3://bucket/ir.json", parameters: list | None = None):
    return {
        "schema_version": "1.0",
        "ir_source": ir_source,
        "parameters": parameters or [],
    }


def _param(parameter: str, value: str, source: str = "parsed", reasoning=None):
    return {"parameter": parameter, "value": value, "source": source, "reasoning": reasoning}


# ---------------------------------------------------------------------------
# Scenario 1: All required values already present
# ---------------------------------------------------------------------------


def test_all_values_present_returns_no_new_params_no_gaps():
    ir = _ir(_service("i1", "aws_instance", label="web_server"))
    manifest = _manifest(parameters=[
        _param("aws_instance.web_server.ami", "ami-0abcdef1234567890"),
        _param("aws_instance.web_server.instance_type", "t3.small"),
    ])

    enriched, gaps = resolve_gaps(ir, manifest)

    assert gaps == []
    # No new parameters added — existing two still there
    assert len(enriched["parameters"]) == 2


def test_all_values_present_does_not_modify_existing_entries():
    ir = _ir(_service("i1", "aws_instance", label="web_server"))
    original_value = "t3.small"
    manifest = _manifest(parameters=[
        _param("aws_instance.web_server.ami", "ami-0abcdef1234567890"),
        _param("aws_instance.web_server.instance_type", original_value),
    ])

    enriched, _ = resolve_gaps(ir, manifest)

    entry = next(
        p for p in enriched["parameters"]
        if p["parameter"] == "aws_instance.web_server.instance_type"
    )
    assert entry["value"] == original_value
    assert entry["source"] == "parsed"


def test_s3_bucket_has_no_required_params_no_gaps():
    ir = _ir(_service("b1", "aws_s3_bucket", label="assets"))
    manifest = _manifest()

    enriched, gaps = resolve_gaps(ir, manifest)

    assert gaps == []
    assert enriched["parameters"] == []


def test_unknown_service_type_is_skipped_without_error():
    ir = _ir(_service("u1", "unknown", label="mystery_box"))
    manifest = _manifest()

    enriched, gaps = resolve_gaps(ir, manifest)

    assert gaps == []
    assert enriched["parameters"] == []


# ---------------------------------------------------------------------------
# Scenario 2: Some parameters missing — defaults applied
# ---------------------------------------------------------------------------


def test_missing_instance_type_gets_agent_default():
    ir = _ir(_service("i1", "aws_instance", label="app_server"))
    manifest = _manifest(parameters=[
        _param("aws_instance.app_server.ami", "ami-0abcdef1234567890"),
    ])

    enriched, gaps = resolve_gaps(ir, manifest)

    assert gaps == []
    new_entry = next(
        p for p in enriched["parameters"]
        if p["parameter"] == "aws_instance.app_server.instance_type"
    )
    assert new_entry["source"] == "agent_default"
    assert new_entry["value"] == "t3.micro"
    assert new_entry["reasoning"] is not None
    assert len(new_entry["reasoning"]) > 0


def test_missing_instance_type_reasoning_mentions_production():
    ir = _ir(_service("i1", "aws_instance", label="app_server"))
    manifest = _manifest(parameters=[
        _param("aws_instance.app_server.ami", "ami-0abcdef1234567890"),
    ])

    enriched, _ = resolve_gaps(ir, manifest)

    entry = next(
        p for p in enriched["parameters"]
        if p["parameter"] == "aws_instance.app_server.instance_type"
    )
    assert "production" in entry["reasoning"].lower() or "review" in entry["reasoning"].lower()


def test_missing_vpc_cidr_gets_agent_default():
    ir = _ir(_service("v1", "aws_vpc", label="main_vpc"))
    manifest = _manifest()

    enriched, gaps = resolve_gaps(ir, manifest)

    assert gaps == []
    entry = next(
        p for p in enriched["parameters"]
        if p["parameter"] == "aws_vpc.main_vpc.cidr_block"
    )
    assert entry["source"] == "agent_default"
    assert entry["value"] == "10.0.0.0/16"


def test_missing_subnet_cidr_gets_agent_default():
    ir = _ir(_service("s1", "aws_subnet", label="public_subnet"))
    manifest = _manifest(parameters=[
        _param("aws_subnet.public_subnet.availability_zone", "us-east-1a"),
    ])

    enriched, gaps = resolve_gaps(ir, manifest)

    assert gaps == []
    entry = next(
        p for p in enriched["parameters"]
        if p["parameter"] == "aws_subnet.public_subnet.cidr_block"
    )
    assert entry["source"] == "agent_default"
    assert "/" in entry["value"]  # It's a CIDR


def test_rds_instance_gets_defaults_for_class_and_storage():
    ir = _ir(_service("db1", "aws_rds_instance", label="primary_db"))
    # Provide the required non-defaultable fields
    manifest = _manifest(parameters=[
        _param("aws_rds_instance.primary_db.engine", "mysql"),
        _param("aws_rds_instance.primary_db.engine_version", "8.0"),
        _param("aws_rds_instance.primary_db.username", "admin"),
        _param("aws_rds_instance.primary_db.password", "supersecret"),
    ])

    enriched, gaps = resolve_gaps(ir, manifest)

    param_keys = [p["parameter"] for p in enriched["parameters"]]
    assert "aws_rds_instance.primary_db.instance_class" in param_keys
    assert "aws_rds_instance.primary_db.allocated_storage" in param_keys

    instance_class = next(
        p for p in enriched["parameters"]
        if p["parameter"] == "aws_rds_instance.primary_db.instance_class"
    )
    assert instance_class["source"] == "agent_default"
    assert instance_class["value"] == "db.t3.micro"


def test_engine_version_defaulted_from_engine_in_manifest():
    ir = _ir(_service("db1", "aws_rds_instance", label="pg_db"))
    manifest = _manifest(parameters=[
        _param("aws_rds_instance.pg_db.engine", "postgres"),
        _param("aws_rds_instance.pg_db.username", "pgadmin"),
        _param("aws_rds_instance.pg_db.password", "secret"),
    ])

    enriched, gaps = resolve_gaps(ir, manifest)

    param_keys = [p["parameter"] for p in enriched["parameters"]]
    assert "aws_rds_instance.pg_db.engine_version" in param_keys

    ev = next(
        p for p in enriched["parameters"]
        if p["parameter"] == "aws_rds_instance.pg_db.engine_version"
    )
    assert ev["source"] == "agent_default"
    assert ev["value"] == "16.1"


def test_ir_config_value_promoted_to_manifest_as_parsed():
    """Values present in IR config but absent from manifest are added as source=parsed."""
    ir = _ir(_service("i1", "aws_instance", label="web", config={"instance_type": "t3.large"}))
    manifest = _manifest(parameters=[
        _param("aws_instance.web.ami", "ami-0abcdef1234567890"),
    ])

    enriched, gaps = resolve_gaps(ir, manifest)

    assert gaps == []
    entry = next(
        p for p in enriched["parameters"]
        if p["parameter"] == "aws_instance.web.instance_type"
    )
    assert entry["source"] == "parsed"
    assert entry["value"] == "t3.large"


def test_manifest_is_append_only_existing_values_unchanged():
    ir = _ir(_service("i1", "aws_instance", label="web"))
    original_params = [
        _param("aws_instance.web.ami", "ami-original"),
        _param("aws_instance.web.instance_type", "t3.xlarge"),
    ]
    manifest = _manifest(parameters=original_params)

    enriched, _ = resolve_gaps(ir, manifest)

    # Original entries must still be exactly as they were
    for orig in original_params:
        match = next(
            p for p in enriched["parameters"] if p["parameter"] == orig["parameter"]
        )
        assert match["value"] == orig["value"]
        assert match["source"] == orig["source"]


def test_multiple_services_all_get_defaults():
    ir = _ir(
        _service("i1", "aws_instance", label="app"),
        _service("b1", "aws_s3_bucket", label="assets"),
        _service("v1", "aws_vpc", label="main"),
    )
    manifest = _manifest(parameters=[
        _param("aws_instance.app.ami", "ami-0abc"),
    ])

    enriched, gaps = resolve_gaps(ir, manifest)

    assert gaps == []
    param_keys = [p["parameter"] for p in enriched["parameters"]]
    assert "aws_instance.app.instance_type" in param_keys
    assert "aws_vpc.main.cidr_block" in param_keys


def test_dynamodb_billing_mode_default():
    ir = _ir(_service("t1", "aws_dynamodb_table", label="orders"))
    manifest = _manifest(parameters=[
        _param("aws_dynamodb_table.orders.hash_key", "order_id"),
    ])

    enriched, gaps = resolve_gaps(ir, manifest)

    assert gaps == []
    entry = next(
        p for p in enriched["parameters"]
        if p["parameter"] == "aws_dynamodb_table.orders.billing_mode"
    )
    assert entry["source"] == "agent_default"
    assert entry["value"] == "PAY_PER_REQUEST"


# ---------------------------------------------------------------------------
# Scenario 3: Unresolvable gaps returned
# ---------------------------------------------------------------------------


def test_missing_ami_returned_as_gap():
    ir = _ir(_service("i1", "aws_instance", label="web_server"))
    manifest = _manifest(parameters=[
        _param("aws_instance.web_server.instance_type", "t3.micro"),
    ])

    _, gaps = resolve_gaps(ir, manifest)

    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["parameter"] == "aws_instance.web_server.ami"
    assert gap["resource_type"] == "aws_instance"
    assert gap["service_id"] == "i1"
    assert len(gap["reason"]) > 0


def test_gap_reason_for_ami_mentions_region():
    ir = _ir(_service("i1", "aws_instance", label="web_server"))
    manifest = _manifest(parameters=[
        _param("aws_instance.web_server.instance_type", "t3.micro"),
    ])

    _, gaps = resolve_gaps(ir, manifest)

    ami_gap = next(g for g in gaps if "ami" in g["parameter"])
    assert "region" in ami_gap["reason"].lower() or "account" in ami_gap["reason"].lower()


def test_lambda_missing_role_and_handler_both_returned_as_gaps():
    ir = _ir(_service("fn1", "aws_lambda_function", label="processor"))
    manifest = _manifest(parameters=[
        _param("aws_lambda_function.processor.runtime", "python3.12"),
    ])

    _, gaps = resolve_gaps(ir, manifest)

    gap_params = [g["parameter"] for g in gaps]
    assert "aws_lambda_function.processor.role" in gap_params
    assert "aws_lambda_function.processor.handler" in gap_params


def test_rds_missing_engine_returned_as_gap():
    ir = _ir(_service("db1", "aws_rds_instance", label="store"))
    manifest = _manifest(parameters=[
        _param("aws_rds_instance.store.username", "admin"),
        _param("aws_rds_instance.store.password", "secret"),
    ])

    _, gaps = resolve_gaps(ir, manifest)

    gap_params = [g["parameter"] for g in gaps]
    assert "aws_rds_instance.store.engine" in gap_params


def test_engine_version_gap_when_engine_unknown():
    """engine_version cannot be defaulted if engine is not known."""
    ir = _ir(_service("db1", "aws_rds_instance", label="store"))
    # engine not provided anywhere
    manifest = _manifest(parameters=[
        _param("aws_rds_instance.store.username", "admin"),
        _param("aws_rds_instance.store.password", "secret"),
        _param("aws_rds_instance.store.engine", "unknown_engine"),
    ])

    _, gaps = resolve_gaps(ir, manifest)

    gap_params = [g["parameter"] for g in gaps]
    assert "aws_rds_instance.store.engine_version" in gap_params


def test_iam_role_assume_role_policy_returned_as_gap():
    ir = _ir(_service("r1", "aws_iam_role", label="lambda_exec"))
    manifest = _manifest()

    _, gaps = resolve_gaps(ir, manifest)

    gap_params = [g["parameter"] for g in gaps]
    assert "aws_iam_role.lambda_exec.assume_role_policy" in gap_params


def test_mixed_services_some_defaults_some_gaps():
    ir = _ir(
        _service("i1", "aws_instance", label="web"),        # ami is a gap, instance_type defaults
        _service("b1", "aws_s3_bucket", label="assets"),    # no required params
        _service("fn1", "aws_lambda_function", label="fn"), # role and handler are gaps, runtime defaults
    )
    manifest = _manifest()

    enriched, gaps = resolve_gaps(ir, manifest)

    param_keys = [p["parameter"] for p in enriched["parameters"]]
    gap_params = [g["parameter"] for g in gaps]

    # Defaults applied
    assert "aws_instance.web.instance_type" in param_keys
    assert "aws_lambda_function.fn.runtime" in param_keys

    # Gaps returned
    assert "aws_instance.web.ami" in gap_params
    assert "aws_lambda_function.fn.role" in gap_params
    assert "aws_lambda_function.fn.handler" in gap_params


def test_gap_entries_have_required_keys():
    ir = _ir(_service("i1", "aws_instance", label="web"))
    manifest = _manifest()

    _, gaps = resolve_gaps(ir, manifest)

    for gap in gaps:
        assert "service_id" in gap
        assert "resource_type" in gap
        assert "parameter" in gap
        assert "reason" in gap
