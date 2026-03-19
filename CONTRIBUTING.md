# Contributing

This project is shared as a portfolio piece and is **not actively maintained as an open source project**. Pull requests and issue reports are not monitored.

That said, you are welcome and encouraged to:

- **Fork it** and build your own agents on top of the architecture
- **Use it as a starting point** for your own Bedrock multi-agent system
- **Adapt the patterns** — the Step Functions pipeline, Lambda layer approach, and action group handler design are all reusable templates

## Good starting points if you want to extend this

**Add a new agent** — the shared infrastructure (EventBridge bus, S3 artifact bucket, Lambda layers bucket) is already in place. Each agent lives under `agents/<agent-name>/` and follows the same structure as the infra-agent.

**Add a new pipeline stage** — the Step Functions state machine in `agents/infra-agent/terraform/main.tf` is straightforward to extend. Add a new Lambda, wire it into the state machine, and give it an IAM role following the existing patterns.

**Swap the model** — change `bedrock_model_id` in `variables.tf`. The `bedrock_base_model_id` local strips the cross-region prefix automatically so IAM ARNs stay correct. See [docs/post-mortem.md](docs/post-mortem.md) for why this matters.

**Add CloudFormation or CDK support** — the validator currently skips non-Terraform code. The layer and pipeline are already parameterized on `iac_type`.

## License

MIT — see [LICENSE](LICENSE). Use the code however you like.
