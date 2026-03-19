import json
import logging
import subprocess
import tempfile
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TERRAFORM_BIN = "/opt/bin/terraform"
TFLINT_BIN = "/opt/bin/tflint"


def lambda_handler(event, context):
    generated_code = event.get("generated_code", "")
    iac_type = event.get("iac_type", "terraform")

    logger.info(json.dumps({"message": "validation_started", "iac_type": iac_type}))

    if iac_type != "terraform":
        logger.info(json.dumps({"message": "validation_skipped", "iac_type": iac_type}))
        return {**event, "validation_status": "skipped", "validation_errors": []}

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
            return {**event, "validation_status": "failed", "validation_errors": errors}

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
        return {**event, "validation_status": status, "validation_errors": errors}
