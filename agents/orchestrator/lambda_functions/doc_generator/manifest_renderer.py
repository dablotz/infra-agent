"""
Renders a configuration manifest into annotated Markdown sections for IaC runbooks.

Produces two sections:
  - Configuration Decisions: a per-resource parameter table with source attribution
  - Assumptions & Review Items: a checklist of agent-chosen values that mention
    production-related language and need operator sign-off before going live
"""
import json
import re
from collections import defaultdict

_PRODUCTION_RE = re.compile(r"\b(prod(?:uction)?|live|release)\b", re.IGNORECASE)


def render(manifest: dict) -> tuple:
    """Return (configuration_decisions_md, assumptions_review_md) as Markdown strings.

    Args:
        manifest: Parsed manifest dict conforming to manifest_schema.json.

    Returns:
        A 2-tuple of Markdown strings.  Both are always populated; the
        Assumptions section carries an explicit all-clear note when there are
        no flagged items.
    """
    parameters = manifest.get("parameters", [])
    config_md = _render_configuration_decisions(parameters)
    assumptions_md = _render_assumptions(parameters)
    return config_md, assumptions_md


# ── internal helpers ──────────────────────────────────────────────────────────

def _resource_key(param_path: str) -> str:
    """Return the resource address portion of a dot-notation parameter path.

    "aws_instance.web_server.instance_type" → "aws_instance.web_server"
    "aws_s3_bucket.bucket"                  → "aws_s3_bucket"
    """
    parts = param_path.rsplit(".", 1)
    return parts[0] if len(parts) > 1 else param_path


def _param_name(param_path: str) -> str:
    """Return the leaf attribute name from a dot-notation parameter path."""
    return param_path.rsplit(".", 1)[-1]


def _format_value(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _notes(entry: dict) -> tuple:
    """Return (notes_text, is_production_flagged) for a single parameter entry."""
    source = entry["source"]
    reasoning = entry.get("reasoning") or ""

    if source == "user_provided":
        return "", False
    if source == "parsed":
        return "Extracted from diagram", False

    # agent_default
    flagged = bool(_PRODUCTION_RE.search(reasoning))
    text = reasoning
    if flagged:
        text = reasoning + " ⚠ Review before production"
    return text, flagged


def _render_configuration_decisions(parameters: list) -> str:
    resource_map: dict = defaultdict(list)
    for entry in parameters:
        resource_map[_resource_key(entry["parameter"])].append(entry)

    lines = ["## Configuration Decisions\n"]

    for resource in sorted(resource_map.keys()):
        lines.append(f"### `{resource}`\n")
        lines.append("| Parameter | Value | Source | Notes |")
        lines.append("|-----------|-------|--------|-------|")

        for entry in resource_map[resource]:
            name = _param_name(entry["parameter"])
            value_str = _format_value(entry["value"])
            source = entry["source"]
            notes_text, _ = _notes(entry)
            lines.append(f"| `{name}` | `{value_str}` | {source} | {notes_text} |")

        lines.append("")  # blank line between resource blocks

    return "\n".join(lines)


def _render_assumptions(parameters: list) -> str:
    flagged = []
    for entry in parameters:
        notes_text, is_flagged = _notes(entry)
        if is_flagged:
            flagged.append({
                "parameter": entry["parameter"],
                "value": _format_value(entry["value"]),
                "notes": notes_text,
            })

    lines = ["## Assumptions & Review Items\n"]

    if not flagged:
        lines.append(
            "_All configuration values were explicitly provided or do not require "
            "production review._\n"
        )
        return "\n".join(lines)

    lines.append(
        "The following parameters were chosen by the agent and reference production "
        "concerns. **Review each entry before deploying to production.**\n"
    )
    lines.append("| Parameter | Value | Notes |")
    lines.append("|-----------|-------|-------|")
    for item in flagged:
        lines.append(f"| `{item['parameter']}` | `{item['value']}` | {item['notes']} |")
    lines.append("")

    return "\n".join(lines)
