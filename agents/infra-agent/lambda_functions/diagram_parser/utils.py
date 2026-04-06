def slugify(label: str, fallback: str = "") -> str:
    """Convert a human label to a Terraform-safe identifier.

    When label is empty, falls back to fallback (e.g. a service's diagram id).
    Both diagram_parser and iac_agent Lambda packages define this function
    independently — they are packaged separately and cannot share a module
    without a Lambda layer. See docs/adr/002-slugify-shared-utility.md.
    """
    if label:
        return label.lower().replace(" ", "_").replace("-", "_").replace(".", "_")
    return fallback.replace("-", "_").lower() if fallback else ""
