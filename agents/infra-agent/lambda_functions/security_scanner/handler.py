import json
import logging
import subprocess
import tempfile
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    generated_code = event.get("generated_code", "")
    iac_type = event.get("iac_type", "terraform")

    logger.info(json.dumps({"message": "security_scan_started", "iac_type": iac_type}))

    with tempfile.TemporaryDirectory() as tmpdir:
        file_ext = ".tf" if iac_type == "terraform" else ".yaml"
        code_file = os.path.join(tmpdir, f"main{file_ext}")
        with open(code_file, "w") as f:
            f.write(generated_code)

        findings = _run_checkov(tmpdir)

    status = "passed" if not findings else "warnings"
    logger.info(json.dumps({
        "message": "security_scan_complete",
        "status": status,
        "finding_count": len(findings),
    }))
    return {
        **event,
        "security_status": status,
        "security_findings": findings,
    }


def _run_checkov(directory: str) -> list:
    """Run checkov on the given directory and return a list of findings.

    Raises RuntimeError on infrastructure failure (checkov not found, timeout, etc.)
    so Step Functions routes the execution to the Failed state rather than silently
    treating an unrun scan as a passing result.
    """
    try:
        result = subprocess.run(
            [
                "python3",
                "-m",
                "checkov.main",
                "-d",
                directory,
                "--output",
                "json",
                "--quiet",
            ],
            capture_output=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        raise RuntimeError(f"Security scan infrastructure failure: {e}") from e

    if not result.stdout:
        return []

    try:
        output = json.loads(result.stdout.decode())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Security scan infrastructure failure: checkov output unparseable: {e}") from e

    findings = []
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
    return findings
