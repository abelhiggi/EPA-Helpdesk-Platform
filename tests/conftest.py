"""Test configuration.

Two things need handling before any handler is imported:

1. boto3 clients are created at module scope, so a region and dummy credentials
   must exist in the environment or the import itself fails.
2. Handlers must be imported fresh per test module so each one gets its own
   mocked clients, so they are loaded by explicit path rather than by name.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("TABLE_NAME", "tickets-test")
os.environ.setdefault("QUEUE_URL", "https://sqs.test/queue")
os.environ.setdefault("DLQ_URL", "https://sqs.test/dlq")
os.environ.setdefault("BEDROCK_MODEL_ID", "test-model")
os.environ.setdefault("NOTIFICATION_FROM", "helpdesk@example.gov.uk")
os.environ.setdefault("NOTIFICATION_TO", "itsupport@example.gov.uk")
os.environ.setdefault("ENVIRONMENT", "test")


def load_handler(name: str):
    """Import src/<name>.py as a uniquely named module."""
    module_name = f"{name}_handler"
    if module_name in sys.modules:
        return sys.modules[module_name]

    path = ROOT / "src" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
