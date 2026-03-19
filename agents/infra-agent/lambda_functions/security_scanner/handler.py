import json
import logging
import subprocess
import tempfile
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)


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

    findings_summary = ""
    if findings:
        lines = [f"- {f['check_id']}: {f['check_name']} (resource: {f['resource']})"
                 for f in findings]
        findings_summary = "\n".join(lines)

    return _response(event, 200, {
        "security_status": status,
        "finding_count": len(findings),
        "findings_summary": findings_summary,
    })


def _run_checkov(directory: str) -> list:
    """Run checkov on the given directory and return a list of findings.

    Raises RuntimeError on infrastructure failure (checkov not found, timeout, etc.)
    so the agent can surface the error rather than silently treating an unrun scan
    as a passing result.
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
