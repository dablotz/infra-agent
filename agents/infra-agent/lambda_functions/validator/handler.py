import json
import logging
import subprocess
import tempfile
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TERRAFORM_BIN = "/opt/bin/terraform"
TFLINT_BIN = "/opt/bin/tflint"


def _get_props(event):
    return (event.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("properties", []))


def _prop(props, name, default=""):
    return next((p["value"] for p in props if p["name"] == name), default)


def _response(event, status_code, body_dict):
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup", ""),
            "apiPath": event.get("apiPath", ""),
            "httpMethod": event.get("httpMethod", "POST"),
            "httpStatusCode": status_code,
            "responseBody": {
                "application/json": {
                    "body": json.dumps(body_dict)
                }
            }
        }
    }


def lambda_handler(event, context):
    props = _get_props(event)
    generated_code = _prop(props, "generated_code")
    iac_type = _prop(props, "iac_type", "terraform")

    if not generated_code:
        return _response(event, 400, {"error": "generated_code is required"})

    logger.info(json.dumps({"message": "validation_started", "iac_type": iac_type}))

    if iac_type != "terraform":
        logger.info(json.dumps({"message": "validation_skipped", "iac_type": iac_type}))
        return _response(event, 200, {
            "validation_status": "skipped",
            "validation_errors": "",
        })

    with tempfile.TemporaryDirectory() as tmpdir:
        tf_file = os.path.join(tmpdir, "main.tf")
        with open(tf_file, "w") as f:
            f.write(generated_code)

        errors = []

        try:
            subprocess.run(
                [TERRAFORM_BIN, "init", "-backend=false"],
                cwd=tmpdir,
                capture_output=True,
                check=True,
                timeout=60,
            )
        except (FileNotFoundError, OSError) as e:
            raise RuntimeError(f"Validation infrastructure failure: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("Validation infrastructure failure: terraform init timed out") from e
        except subprocess.CalledProcessError as e:
            errors.append(f"Terraform init failed: {e.stderr.decode()}")
            logger.warning(json.dumps({"message": "terraform_init_failed"}))
            return _response(event, 200, {
                "validation_status": "failed",
                "validation_errors": "\n".join(errors),
            })

        try:
            subprocess.run(
                [TERRAFORM_BIN, "validate"],
                cwd=tmpdir,
                capture_output=True,
                check=True,
                timeout=60,
            )
        except (FileNotFoundError, OSError) as e:
            raise RuntimeError(f"Validation infrastructure failure: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("Validation infrastructure failure: terraform validate timed out") from e
        except subprocess.CalledProcessError as e:
            errors.append(f"Terraform validate failed: {e.stderr.decode()}")

        try:
            subprocess.run(
                [TFLINT_BIN, "--init"],
                cwd=tmpdir,
                capture_output=True,
                check=True,
                timeout=60,
            )
            result = subprocess.run(
                [TFLINT_BIN], cwd=tmpdir, capture_output=True, timeout=60
            )
            if result.returncode != 0:
                errors.append(f"tflint warnings: {result.stdout.decode()}")
        except (FileNotFoundError, OSError) as e:
            raise RuntimeError(f"Validation infrastructure failure: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("Validation infrastructure failure: tflint timed out") from e
        except subprocess.CalledProcessError as e:
            errors.append(f"tflint failed: {str(e)}")

        status = "passed" if not errors else "failed"
        logger.info(json.dumps({
            "message": "validation_complete",
            "status": status,
            "error_count": len(errors),
        }))
        return _response(event, 200, {
            "validation_status": status,
            "validation_errors": "\n".join(errors),
        })
