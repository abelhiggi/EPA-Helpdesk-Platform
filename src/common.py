"""Structured logging and custom metrics.

Shared by all three handlers. They live in one asset directory so this file is
importable without being copied, layered or packaged — the Lambda runtime puts
the asset root on sys.path, so `from common import ...` just works.

Metrics go out as CloudWatch Embedded Metric Format: a specially shaped log
line that CloudWatch converts into a metric. That means no PutMetricData
permission on the runtime role and no synchronous API call in the request path.

Never logged: ticket descriptions, email addresses, tokens, raw events.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

SERVICE = os.environ.get("SERVICE_NAME", "helpdesk")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
NAMESPACE = os.environ.get("METRIC_NAMESPACE", "EPA/Helpdesk")

STATUS_NEW = "NEW"
STATUS_ROUTED = "ROUTED"
STATUS_FAILED = "FAILED"

CATEGORIES = ("network", "software")
PRIORITIES = ("low", "medium", "high")
TEAM_BY_CATEGORY = {"network": "Network Team", "software": "Software Team"}

_NEVER_LOG = {"description", "requesteremail", "authorization", "token", "password"}


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_id() -> str:
    return str(uuid.uuid4())


def seconds_between(start_iso: str, end_iso: str) -> float | None:
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return max((end - start).total_seconds(), 0.0)
    except (ValueError, AttributeError, TypeError):
        return None


def _safe(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None and k.lower() not in _NEVER_LOG}


def log(level: str, message: str, **fields: Any) -> None:
    print(
        json.dumps(
            {
                "timestamp": now_iso(),
                "level": level.upper(),
                "service": SERVICE,
                "environment": ENVIRONMENT,
                "message": message,
                **_safe(fields),
            },
            default=str,
        ),
        file=sys.stdout,
    )


def info(message: str, **fields: Any) -> None:
    log("INFO", message, **fields)


def warn(message: str, **fields: Any) -> None:
    log("WARN", message, **fields)


def error(message: str, **fields: Any) -> None:
    log("ERROR", message, **fields)


def emit_metric(
    name: str,
    value: float,
    unit: str = "Count",
    dimensions: dict[str, str] | None = None,
    **properties: Any,
) -> None:
    """Emit one custom metric via Embedded Metric Format.

    Dimensions must stay low cardinality — Category and Priority only. Putting
    ticketId in a dimension would create a new metric per ticket and a bill to
    match.
    """
    dims = {"Service": SERVICE, "Environment": ENVIRONMENT}
    if dimensions:
        dims.update({k: v for k, v in dimensions.items() if v})

    record: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [list(dims.keys())],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        **dims,
        name: value,
        **_safe(properties),
    }
    print(json.dumps(record, default=str), file=sys.stdout)


def response(status: int, body: dict[str, Any], origin: str = "*") -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": origin,
        },
        "body": json.dumps(body),
    }


def requester_from_claims(event: dict[str, Any]) -> str | None:
    """Identity comes from the verified Cognito token, never the request body.

    A body-supplied email would let any authenticated user raise a ticket as
    somebody else.
    """
    try:
        claims = event["requestContext"]["authorizer"]["claims"]
    except (KeyError, TypeError):
        return None
    return claims.get("email") or claims.get("cognito:username")
