# ADR-002: Per-Package `slugify` Utility Over a Lambda Layer

**Status**: Accepted
**Date**: 2026-04-06
**Deciders**: Greg Blotzer

---

## Context

Four near-identical implementations of a slug-conversion function existed across the codebase:

| File | Function |
|---|---|
| `diagram_parser/handler.py` | `_slugify(label)` |
| `diagram_parser/png_pipeline/handler.py` | `_slugify(label)` (duplicate) |
| `iac_agent/gap_resolver.py` | `_service_slug(service)` (same logic + fallback) |
| `iac_agent/terraform_prompt_builder.py` | inline (same logic + fallback) |

All convert a human-readable label to a Terraform-safe identifier by lowercasing and replacing spaces, hyphens, and dots with underscores.

Two approaches to consolidation were evaluated.

### Option A — Lambda Layer (rejected)

Create a `common-utils` Lambda layer in `shared/lambda_layers/` containing a single authoritative `slugify` implementation. The CDK would build the layer, version it, and attach it to `diagram_parser` and `iac_agent`.

- **Pro**: one file, zero duplication
- **Con**: adds a Docker-based layer build step, a new CDK `LayerVersion` construct, and layer attachment wiring across two stacks — significant infrastructure overhead for a 3-line pure-Python function with stable logic

### Option B — Per-Package `utils.py` (chosen)

Create one `utils.py` per Lambda deployment package:

- `diagram_parser/utils.py` — used by `handler.py` and `png_pipeline/handler.py` (co-packaged)
- `iac_agent/utils.py` — used by `gap_resolver.py` and `terraform_prompt_builder.py`

The logic is written in two files, but each file is the single authoritative definition within its deployment unit. The duplication is bounded and explicit.

---

## Decision

**Option B — per-package `utils.py`** was chosen.

The function is 3 lines of pure Python with no dependencies. Its logic is unlikely to change (it mirrors Terraform's own identifier rules). A Lambda layer is the correct pattern when shared code is complex, frequently updated, or used across many functions; none of those conditions apply here.

---

## Consequences

- `diagram_parser/utils.py` and `iac_agent/utils.py` must stay in sync. A comment in `iac_agent/utils.py` documents this and references this ADR.
- If additional shared utilities accumulate (more than one or two functions across these packages), the Lambda layer trade-off should be re-evaluated at that time.

---

## When to Revisit

Switch to a Lambda layer if any of the following occur:

- More than two distinct utility functions need to be shared across Lambda packages
- The slug conversion logic needs to change and the two-file update becomes error-prone
- A third Lambda package needs access to the same function
