import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "lambda_functions", "doc_generator"
    ),
)

import manifest_renderer


# ── fixture helpers ────────────────────────────────────────────────────────────

def _make_manifest(parameters):
    return {
        "schema_version": "1.0",
        "ir_source": "diagrams/test/ir.json",
        "parameters": parameters,
    }


def _user(path, value):
    return {"parameter": path, "value": value, "source": "user_provided", "reasoning": None}


def _parsed(path, value):
    return {"parameter": path, "value": value, "source": "parsed", "reasoning": None}


def _default(path, value, reasoning):
    return {"parameter": path, "value": value, "source": "agent_default", "reasoning": reasoning}


# ── all user_provided ──────────────────────────────────────────────────────────

class TestAllUserProvided:
    def setup_method(self):
        manifest = _make_manifest([
            _user("aws_instance.web.instance_type", "t3.medium"),
            _user("aws_instance.web.ami", "ami-0abcdef1234567890"),
            _user("aws_s3_bucket.assets.bucket", "my-assets-bucket"),
        ])
        self.config_md, self.assumptions_md = manifest_renderer.render(manifest)

    def test_config_section_header_present(self):
        assert "## Configuration Decisions" in self.config_md

    def test_resource_subsections_present(self):
        assert "`aws_instance.web`" in self.config_md
        assert "`aws_s3_bucket.assets`" in self.config_md

    def test_user_provided_notes_column_empty(self):
        # Each row for user_provided must have an empty notes cell.
        # Table rows look like: | `param` | `value` | user_provided |  |
        for line in self.config_md.splitlines():
            if "user_provided" in line:
                cells = [c.strip() for c in line.split("|")]
                notes_cell = cells[-2]  # last non-empty cell is notes
                assert notes_cell == "", f"Expected empty notes, got: {notes_cell!r}"

    def test_assumptions_section_all_clear(self):
        assert "## Assumptions & Review Items" in self.assumptions_md
        assert "explicitly provided" in self.assumptions_md
        assert "production review" in self.assumptions_md

    def test_assumptions_section_no_table(self):
        assert "|" not in self.assumptions_md


# ── all parsed ────────────────────────────────────────────────────────────────

class TestAllParsed:
    def setup_method(self):
        manifest = _make_manifest([
            _parsed("aws_vpc.main.cidr_block", "10.0.0.0/16"),
            _parsed("aws_subnet.public.cidr_block", "10.0.1.0/24"),
        ])
        self.config_md, self.assumptions_md = manifest_renderer.render(manifest)

    def test_parsed_notes_column(self):
        for line in self.config_md.splitlines():
            if "parsed" in line and line.strip().startswith("|"):
                assert "Extracted from diagram" in line

    def test_assumptions_all_clear(self):
        assert "explicitly provided" in self.assumptions_md
        assert "|" not in self.assumptions_md


# ── all agent_default ─────────────────────────────────────────────────────────

class TestAllAgentDefault:
    def setup_method(self):
        self.non_prod_reasoning = (
            "Chosen based on cost-efficiency for a dev workload. "
            "Consider upgrading for higher traffic."
        )
        self.prod_reasoning = (
            "Defaulting to Multi-AZ for production reliability. "
            "Single-AZ is cheaper but unsuitable for production."
        )
        manifest = _make_manifest([
            _default("aws_db_instance.main.instance_class", "db.t3.medium", self.non_prod_reasoning),
            _default("aws_db_instance.main.multi_az", True, self.prod_reasoning),
        ])
        self.config_md, self.assumptions_md = manifest_renderer.render(manifest)

    def test_reasoning_in_notes(self):
        assert self.non_prod_reasoning in self.config_md
        assert self.prod_reasoning in self.config_md

    def test_production_flag_appended(self):
        # The prod_reasoning entry should have the warning appended.
        for line in self.config_md.splitlines():
            if "multi_az" in line:
                assert "⚠ Review before production" in line

    def test_non_production_reasoning_no_flag(self):
        for line in self.config_md.splitlines():
            if "instance_class" in line:
                assert "⚠ Review before production" not in line

    def test_assumptions_table_has_flagged_entry(self):
        assert "## Assumptions & Review Items" in self.assumptions_md
        assert "multi_az" in self.assumptions_md
        assert "⚠ Review before production" in self.assumptions_md

    def test_assumptions_table_excludes_non_flagged(self):
        # instance_class should not appear in the assumptions section.
        assert "instance_class" not in self.assumptions_md

    def test_assumptions_table_has_header_row(self):
        assert "| Parameter | Value | Notes |" in self.assumptions_md


# ── mixed manifest ────────────────────────────────────────────────────────────

class TestMixedManifest:
    def setup_method(self):
        manifest = _make_manifest([
            _user("aws_instance.app.instance_type", "t3.large"),
            _parsed("aws_instance.app.ami", "ami-abc123"),
            _default(
                "aws_instance.app.monitoring",
                True,
                "Enabled detailed monitoring for production observability.",
            ),
            _default(
                "aws_instance.app.ebs_optimized",
                True,
                "Enabled by default for better I/O throughput on this instance type.",
            ),
        ])
        self.config_md, self.assumptions_md = manifest_renderer.render(manifest)

    def test_all_three_sources_present(self):
        assert "user_provided" in self.config_md
        assert "parsed" in self.config_md
        assert "agent_default" in self.config_md

    def test_user_row_empty_notes(self):
        for line in self.config_md.splitlines():
            if "instance_type" in line:
                cells = [c.strip() for c in line.split("|")]
                assert cells[-2] == ""

    def test_parsed_row_extracted_note(self):
        for line in self.config_md.splitlines():
            if "ami" in line and line.strip().startswith("|"):
                assert "Extracted from diagram" in line

    def test_production_flagged_entry_in_assumptions(self):
        assert "monitoring" in self.assumptions_md

    def test_non_production_default_not_in_assumptions(self):
        assert "ebs_optimized" not in self.assumptions_md

    def test_config_grouped_under_single_resource(self):
        # All four params share the aws_instance.app resource address.
        assert self.config_md.count("`aws_instance.app`") == 1


# ── edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_manifest_parameters(self):
        manifest = _make_manifest([])
        config_md, assumptions_md = manifest_renderer.render(manifest)
        assert "## Configuration Decisions" in config_md
        assert "## Assumptions & Review Items" in assumptions_md
        assert "explicitly provided" in assumptions_md

    def test_production_keyword_variants(self):
        """prod, production, live, release all trigger the flag."""
        keywords = [
            "Chosen for prod deployments.",
            "Suitable for production environments.",
            "Used in live traffic routing.",
            "Default for release branches.",
        ]
        for reasoning in keywords:
            manifest = _make_manifest([
                _default("aws_lambda.fn.timeout", 30, reasoning)
            ])
            _, assumptions_md = manifest_renderer.render(manifest)
            assert "⚠ Review before production" in assumptions_md, (
                f"Expected flag for reasoning: {reasoning!r}"
            )

    def test_boolean_value_formatted_lowercase(self):
        manifest = _make_manifest([_user("aws_instance.x.ebs_optimized", True)])
        config_md, _ = manifest_renderer.render(manifest)
        assert "`true`" in config_md

    def test_dict_value_json_serialized(self):
        manifest = _make_manifest([_user("aws_iam_policy.p.tags", {"env": "dev"})])
        config_md, _ = manifest_renderer.render(manifest)
        assert '{"env": "dev"}' in config_md

    def test_resources_sorted_alphabetically(self):
        manifest = _make_manifest([
            _user("aws_s3_bucket.z_bucket.bucket", "z"),
            _user("aws_ec2_instance.a_server.ami", "ami-123"),
        ])
        config_md, _ = manifest_renderer.render(manifest)
        z_pos = config_md.index("aws_s3_bucket.z_bucket")
        a_pos = config_md.index("aws_ec2_instance.a_server")
        assert a_pos < z_pos

    def test_single_segment_parameter_path(self):
        """Parameters without a dot should not crash the renderer."""
        manifest = _make_manifest([
            _user("region", "us-east-1"),
        ])
        config_md, _ = manifest_renderer.render(manifest)
        assert "## Configuration Decisions" in config_md
