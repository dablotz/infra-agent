"""
Terraform prompt builder for the diagram-to-IaC pipeline.

Takes a fully resolved IR and manifest and constructs the Bedrock prompt that
instructs Claude to generate valid Terraform HCL.

Rules enforced in the generated prompt:
  - Use exact parameter values from the manifest (no assumptions).
  - Add an inline HCL comment on every agent_default value with its reasoning.
  - Emit one resource block per service in the IR.
  - Follow the resource relationships from the IR when writing references.
"""

import json

from utils import slugify


def _build_param_block(resource_type: str, service_slug: str, parameters: list[dict]) -> str:
    """Return a formatted block of resolved parameters for a single resource.

    Lines for agent_default entries include the reasoning so the prompt can
    instruct the model to echo it as an HCL inline comment.
    """
    prefix = f"{resource_type}.{service_slug}."
    lines: list[str] = []
    for p in parameters:
        if not p["parameter"].startswith(prefix):
            continue
        attr = p["parameter"][len(prefix):]
        value = p["value"]
        source = p.get("source", "")
        reasoning = p.get("reasoning") or ""

        if source == "agent_default":
            lines.append(f'  {attr} = "{value}"  # AGENT DEFAULT: {reasoning}')
        else:
            lines.append(f'  {attr} = "{value}"')
    return "\n".join(lines)


def _build_relationship_notes(ir: dict) -> str:
    """Summarise relationships as a plain-English instruction block."""
    rels = ir.get("relationships", [])
    if not rels:
        return ""
    notes: list[str] = []
    for r in rels:
        notes.append(
            f"  - {r['source']} {r['relationship_type'].replace('_', ' ')} {r['target']}"
            + (f" ({r['label']})" if r.get("label") else "")
        )
    return (
        "Use the following resource relationships to wire Terraform references "
        "(e.g. aws_vpc.x.id, aws_subnet.y.id) instead of hard-coded IDs:\n"
        + "\n".join(notes)
    )


def build_prompt(ir: dict, manifest: dict) -> str:
    """Construct the Bedrock prompt for Terraform HCL generation.

    Args:
        ir:       Fully resolved IR dict conforming to ir_schema.json.
        manifest: Fully resolved manifest dict (all gaps filled) conforming to
                  manifest_schema.json.

    Returns:
        A raw prompt string ready to be sent as the user message to Bedrock.
    """
    parameters = manifest.get("parameters", [])
    services = ir.get("services", [])
    relationship_notes = _build_relationship_notes(ir)

    # Build a human-readable resource list with resolved params
    resource_blocks: list[str] = []
    for service in services:
        resource_type = service.get("type", "unknown")
        if resource_type == "unknown":
            continue
        label = service.get("label", "")
        slug = slugify(label, fallback=service["id"])
        param_block = _build_param_block(resource_type, slug, parameters)
        block = f"Resource: {resource_type} (name: {slug})"
        if param_block:
            block += f"\nResolved parameters:\n{param_block}"
        resource_blocks.append(block)

    resources_section = "\n\n".join(resource_blocks) if resource_blocks else "(no resources)"

    # Collect agent_default values so we can remind the model to comment them
    agent_defaults = [
        p for p in parameters if p.get("source") == "agent_default"
    ]
    defaults_reminder = ""
    if agent_defaults:
        defaults_reminder = (
            "\nAGENT DEFAULT VALUES — add an inline HCL comment on each of these "
            "attributes explaining the reasoning:\n"
            + "\n".join(
                f"  - {p['parameter']}: \"{p['value']}\"  # {p['reasoning']}"
                for p in agent_defaults
            )
            + "\n"
        )

    return f"""Generate valid Terraform HCL for the following AWS infrastructure.

IMPORTANT RULES:
- Use EXACTLY the parameter values listed under each resource. Do not substitute or invent values.
- For every attribute marked with '# AGENT DEFAULT:', copy that comment as an inline HCL comment on the same line in the output, so readers know it needs review.
- Emit ONE resource block per resource listed below. Do not merge or split resources.
- Add a required_providers block and a provider "aws" block with region = var.aws_region.
- Declare a variable "aws_region" with default = "us-east-1".
- Use Terraform resource references (e.g. aws_vpc.main.id) instead of hard-coded IDs whenever a relationship exists between resources.
- Do NOT add extra networking, IAM, or monitoring resources beyond what is listed.
- Output ONLY raw HCL — no markdown fences, no explanations, no prose.
- Start directly with the terraform {{ block.

RESOURCES TO GENERATE:
{resources_section}

{relationship_notes}
{defaults_reminder}
Terraform HCL:"""
