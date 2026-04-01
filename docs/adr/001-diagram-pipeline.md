# ADR-001: Diagram Pipeline as a Preprocessing Step

**Status**: Accepted
**Date**: 2026-04-01
**Deciders**: Greg Blotzer

---

## Context

The infra-agent application generates validated, security-scanned Infrastructure as Code from
natural language descriptions. A new capability is required: the ability to accept uploaded
architecture diagrams (draw.io / Lucidchart XML, PNG, JPG) and produce IaC from them.

Two integration approaches were considered:

### Option A — Dedicated Diagram Agent (rejected)

Add a third Bedrock agent specifically for diagram processing. It would be registered as a
collaborator alongside the existing infra-agent. The orchestrator would route diagram requests
to it, receive the IR back, then route the IR to the infra-agent.

### Option B — Preprocessing Step (chosen)

Place diagram parsing *before* the orchestrating agent is invoked. A lightweight S3-triggered
Lambda (`upload_router`) detects uploads, routes to the correct parser (XML or PNG), and
injects the resulting IR and manifest paths into the user message as a structured
`[DIAGRAM_CONTEXT]` block. The orchestrating agent receives an enriched but otherwise normal
request and delegates to the infra-agent exactly as it does for text-only requests.

---

## Decision

**Option B — Preprocessing Step** was chosen.

### Reasoning

1. **Minimal blast radius on the existing flow.** The orchestrator and infra-agent already
   handle text-only requests correctly. Option B adds diagram support via a single new entry
   point (`upload_router`) and two instruction-set updates. No new Bedrock agent is created,
   no new collaboration chain is introduced, and the text-only path is entirely unchanged.

2. **Lower latency.** A dedicated diagram agent would add a full Bedrock agent round-trip
   (routing decision + agent initialization + tool calls) before the IR is even produced.
   The preprocessing step invokes the parser Lambda synchronously and passes the result
   directly to the orchestrator in a single call, eliminating that overhead.

3. **Simpler failure modes.** With Option A, a failure in the diagram agent produces an
   ambiguous error inside the Bedrock collaboration chain that is difficult to surface
   clearly. Option B fails fast at the Lambda level with a structured HTTP status code
   before the Bedrock agents are involved.

4. **Cost.** Each Bedrock agent invocation carries a per-request charge and increases
   token consumption (system prompts, tool definitions). Option B adds one Lambda invocation
   per upload instead of a second full agent session.

5. **Separation of concerns is preserved without a new agent.** The XML parser and PNG
   pipeline Lambdas are already standalone, testable units. The upload router is a thin
   coordinator. The IR/manifest contract (`schemas/ir_schema.json`,
   `schemas/manifest_schema.json`) is the stable interface between diagram parsing and IaC
   generation — no agent boundary is needed to enforce it.

---

## Consequences

- The `[DIAGRAM_CONTEXT]` block in the user message is an implicit protocol between the
  upload router and the orchestrator's system prompt. Changes to that format must be
  coordinated across `upload_router.py` and both agent instruction files.

- The orchestrator and infra-agent instructions carry diagram-specific logic (parsing the
  `[DIAGRAM_CONTEXT]` block, branching on the presence of `ir_path`). This coupling is
  acceptable at the current scale but should be revisited if instruction length starts to
  affect agent reliability (see *When to Revisit* below).

- Text-only requests see no behavioral change. The `ir_path` and `manifest_path` fields
  are optional throughout the pipeline; all null checks are explicit.

- The `ProcessDiagram` action group (backed by `iac_agent/handler.py`) is the only new
  action group added to the infra-agent. It reuses the existing `gap_resolver` and
  `terraform_prompt_builder` modules, keeping the diagram path consistent with the rest
  of the IaC generation logic.

---

## When to Revisit

Consider switching to a dedicated diagram agent (Option A) if any of the following occur:

- **Instruction drift**: The orchestrator or infra-agent system prompts grow beyond ~2,000
  tokens due to diagram-specific branching logic, causing the agent to mis-route requests.

- **Multi-format expansion**: Diagram support expands to formats (e.g. Visio, Mermaid,
  CloudFormation Designer) that each require their own parser Lambda *and* their own
  IR normalisation strategy, making the routing logic in `upload_router.py` non-trivial.

- **Independent scalability**: The diagram parsing workload grows large enough that it
  benefits from independent scaling, concurrency limits, or a separate deployment lifecycle
  from the rest of the orchestration layer.

- **Cross-team ownership**: A separate team takes ownership of diagram parsing. A dedicated
  agent provides a clean API boundary with its own deployment pipeline and on-call rotation.

In that event, the IR/manifest schema contract and the `[DIAGRAM_CONTEXT]` message format
provide natural seams for extracting the diagram capability into its own agent without
restructuring the infra-agent or doc-generation pipeline.
