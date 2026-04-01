# Contributing

This project is shared as a portfolio piece and is **not actively maintained as an open source project**. Pull requests and issue reports are not monitored.

That said, you are welcome and encouraged to:

- **Fork it** and build your own agents on top of the architecture
- **Use it as a starting point** for your own Bedrock multi-agent system
- **Adapt the patterns** — the CDK stack structure, Lambda layer approach, and action group handler design are all reusable templates

## Good starting points if you want to extend this

**Add a new agent** — the shared infrastructure (EventBridge bus, S3 artifact bucket, Lambda layers bucket) is already in place. Each agent lives under `agents/<agent-name>/` and follows the same structure as the infra-agent.

**Add a new pipeline stage** — add a new Lambda handler under `agents/infra-agent/lambda_functions/`, wire it into `cdk/stacks/infra_agent_stack.py` following the existing patterns, and add an action group schema under `agents/infra-agent/bedrock/`.

**Add a new diagram format** — add a parser function to `diagram_parser/handler.py` (or create a new Lambda for complex formats), register the file extension in `upload_router.py`, and update the S3 event filter in `diagram_pipeline_stack.py`. The IR and manifest schemas (`schemas/ir_schema.json`, `schemas/manifest_schema.json`) are the stable contract — as long as your parser produces valid output against them, the rest of the pipeline requires no changes.

**Swap the model** — change `BEDROCK_MODEL_ID` in the relevant CDK stack (`cdk/stacks/infra_agent_stack.py`, `cdk/stacks/orchestrator_stack.py`, or `cdk/stacks/diagram_pipeline_stack.py`). The `BEDROCK_BASE_MODEL_ID` environment variable is derived automatically by stripping the cross-region prefix so IAM ARNs stay correct. See [docs/post-mortem.md](docs/post-mortem.md) for why this matters.

**Add CloudFormation or CDK support** — the validator currently skips non-Terraform code. The layer and pipeline are already parameterized on `iac_type`.

## License

MIT — see [LICENSE](LICENSE). Use the code however you like.
