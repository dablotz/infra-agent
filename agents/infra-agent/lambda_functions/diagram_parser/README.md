# Diagram Parser Lambda

Converts draw.io (`.drawio`) and Lucidchart (`.xml`) architecture diagrams to a
**Normalized Intermediate Representation (IR)** and an initial **Configuration Manifest**,
both written back to the same S3 bucket.

## Trigger

S3 event (`s3:ObjectCreated:*`) on `.drawio` or `.xml` objects.

## Outputs

| File | Path |
|------|------|
| IR JSON | `diagrams/{stem}/ir.json` |
| Manifest JSON | `diagrams/{stem}/manifest.json` |

Both schemas are defined in `schemas/ir_schema.json` and `schemas/manifest_schema.json`
at the project root.

---

## Extending the Shape Mapping

`SHAPE_TO_TERRAFORM` in `handler.py` maps normalised shape identifiers to Terraform
resource types. It is the single place to extend when you encounter an unmapped service
(which will appear as `"type": "unknown"` in the IR).

### Step 1 — Find the raw shape name

**draw.io:** Open the `.drawio` file as plain text (it is XML). Find the `mxCell` for
the unmapped shape and copy the `style` attribute. Look for either:

```
resIcon=mxgraph.aws4.transfer_family
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ — this is the raw value
```

or (simpler shapes):

```
shape=mxgraph.aws4.transfer_family
       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
```

**Lucidchart:** Open the exported XML and find the `<element>` tag for the unmapped
service. Copy the `type` attribute:

```xml
<element id="..." type="aws.TransferFamily">
                         ^^^^^^^^^^^^^^^^^^^ — this is the raw value
```

### Step 2 — Normalise the key

Strip the `mxgraph.` prefix and lower-case the result:

| Raw value | Normalised key |
|-----------|----------------|
| `mxgraph.aws4.transfer_family` | `aws4.transfer_family` |
| `aws.TransferFamily` | `aws.transferfamily` |

### Step 3 — Add the entry

Open `handler.py` and add a line to the appropriate section of `SHAPE_TO_TERRAFORM`:

```python
SHAPE_TO_TERRAFORM: dict[str, str] = {
    # --- Storage ---
    "aws4.s3": "aws_s3_bucket",
    "aws4.transfer_family": "aws_transfer_server",   # <-- new entry
    ...
}
```

The value must be the Terraform resource type exactly as it appears in the
[Terraform AWS Provider documentation](https://registry.terraform.io/providers/hashicorp/aws/latest/docs).

### Step 4 — Add a test

Add a line to the parametrized `test_shape_to_terraform_known_mappings` test in
`tests/unit/test_diagram_parser.py`:

```python
("aws4.transfer_family", "aws_transfer_server"),
```

Run the tests to confirm nothing regressed:

```bash
pytest agents/infra-agent/tests/unit/test_diagram_parser.py -v
```
