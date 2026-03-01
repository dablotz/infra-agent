import subprocess
import tempfile
import os


def lambda_handler(event, context):
    generated_code = event.get("generated_code", "")
    iac_type = event.get("iac_type", "terraform")

    if iac_type != "terraform":
        return {**event, "validation_status": "skipped", "validation_errors": []}

    with tempfile.TemporaryDirectory() as tmpdir:
        tf_file = os.path.join(tmpdir, "main.tf")
        with open(tf_file, "w") as f:
            f.write(generated_code)

        errors = []

        try:
            subprocess.run(
                ["/opt/bin/terraform", "init", "-backend=false"],
                cwd=tmpdir,
                capture_output=True,
                check=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as e:
            errors.append(f"Terraform init failed: {e.stderr.decode()}")
            return {**event, "validation_status": "failed", "validation_errors": errors}

        try:
            subprocess.run(
                ["/opt/bin/terraform", "validate"],
                cwd=tmpdir,
                capture_output=True,
                check=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as e:
            errors.append(f"Terraform validate failed: {e.stderr.decode()}")

        try:
            subprocess.run(
                ["/opt/bin/tflint", "--init"],
                cwd=tmpdir,
                capture_output=True,
                check=True,
                timeout=60,
            )
            result = subprocess.run(
                ["/opt/bin/tflint"], cwd=tmpdir, capture_output=True, timeout=60
            )
            if result.returncode != 0:
                errors.append(f"tflint warnings: {result.stdout.decode()}")
        except Exception as e:
            errors.append(f"tflint failed: {str(e)}")

        status = "passed" if not errors else "failed"
        return {**event, "validation_status": status, "validation_errors": errors}
