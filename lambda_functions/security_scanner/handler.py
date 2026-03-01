import json
import subprocess
import tempfile
import os


def lambda_handler(event, context):
    generated_code = event.get("generated_code", "")
    iac_type = event.get("iac_type", "terraform")

    with tempfile.TemporaryDirectory() as tmpdir:
        file_ext = ".tf" if iac_type == "terraform" else ".yaml"
        code_file = os.path.join(tmpdir, f"main{file_ext}")
        with open(code_file, "w") as f:
            f.write(generated_code)

        findings = []

        try:
            result = subprocess.run(
                [
                    "python3",
                    "-m",
                    "checkov.main",
                    "-d",
                    tmpdir,
                    "--output",
                    "json",
                    "--quiet",
                ],
                capture_output=True,
                timeout=120,
            )

            if result.stdout:
                output = json.loads(result.stdout.decode())
                for check_result in output.get("results", {}).get("failed_checks", []):
                    findings.append(
                        {
                            "check_id": check_result.get("check_id"),
                            "check_name": check_result.get("check_name"),
                            "severity": check_result.get("severity", "MEDIUM"),
                            "resource": check_result.get("resource"),
                            "guideline": check_result.get("guideline"),
                        }
                    )
        except Exception as e:
            findings.append({"error": f"Security scan failed: {str(e)}"})

        return {
            **event,
            "security_status": "passed" if not findings else "warnings",
            "security_findings": findings,
        }
