"""
Shared pytest fixtures and utilities for the infra-agent Lambda test suite.
"""

import importlib.util
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

LAMBDA_BASE = pathlib.Path(__file__).parent.parent / "lambda_functions"

# Add each Lambda package directory to sys.path so that package-local imports
# (e.g. `from utils import slugify`) resolve the same way they do at Lambda
# runtime, where the function's directory is the working directory.
_LAMBDA_PACKAGES = ["diagram_parser", "iac_agent", "artifact_uploader",
                    "code_generator", "validator", "security_scanner"]
for _pkg in _LAMBDA_PACKAGES:
    _pkg_dir = str(LAMBDA_BASE / _pkg)
    if _pkg_dir not in sys.path:
        sys.path.insert(0, _pkg_dir)


def load_handler(lambda_name: str):
    """Load a Lambda handler module by its directory name.

    Uses a unique module name to prevent sys.modules collisions when multiple
    handler modules (all named handler.py) are loaded in the same pytest session.
    """
    path = LAMBDA_BASE / lambda_name / "handler.py"
    spec = importlib.util.spec_from_file_location(lambda_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lambda_context():
    """Mock AWS Lambda context object."""
    ctx = MagicMock()
    ctx.function_name = "test-function"
    ctx.function_version = "$LATEST"
    ctx.invoked_function_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:test-function"
    )
    ctx.memory_limit_in_mb = 512
    ctx.aws_request_id = "test-request-id-0000"
    ctx.log_group_name = "/aws/lambda/test-function"
    ctx.log_stream_name = "2024/01/01/[$LATEST]test"
    ctx.get_remaining_time_in_millis = lambda: 30000
    return ctx
