"""API handler: accept a ticket, or return one's status.

Both routes live in one function because they share validation, identity
extraction and table access. Splitting them would mean two cold starts, two
roles and two sets of tests for about forty lines of logic.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

from common import (
    STATUS_NEW,
    emit_metric,
    error,
    info,
    new_id,
    now_iso,
    requester_from_claims,
    response,
    warn,
)

TABLE_NAME = os.environ["TABLE_NAME"]
QUEUE_URL = os.environ["QUEUE_URL"]
MAX_DESCRIPTION = 4000

_dynamodb = boto3.resource("dynamodb")
_sqs = boto3.client("sqs")
_table = _dynamodb.Table(TABLE_NAME)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    # Unauthenticated by design: reports API reachability, not identity, so it
    # must resolve before the Cognito claims check below.
    if event.get("resource") == "/health":
        return response(200, {"status": "ok"})

    method = event.get("httpMethod", "")
    requester = requester_from_claims(event)

    if not requester:
        warn("request rejected: no verified identity in token")
        return response(401, {"error": "Unauthorised"})

    if method == "POST":
        return _create(event, requester)
    if method == "GET":
        return _read(event, requester)
    return response(405, {"error": f"Method {method} not allowed"})


def _create(event: dict[str, Any], requester: str) -> dict[str, Any]:
    correlation_id = new_id()

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return response(400, {"error": "Body must be valid JSON"})

    description = (body.get("description") or "").strip()
    if not description:
        return response(400, {"error": "description is required"})
    if len(description) > MAX_DESCRIPTION:
        return response(
            400, {"error": f"description must be {MAX_DESCRIPTION} characters or fewer"}
        )

    ticket_id = new_id()
    created_at = now_iso()

    item = {
        "ticketId": ticket_id,
        "status": STATUS_NEW,
        "createdAt": created_at,
        "description": description,
        "requesterEmail": requester,
        "correlationId": correlation_id,
    }

    try:
        _table.put_item(Item=item)
        _sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps(
                {
                    "ticketId": ticket_id,
                    "correlationId": correlation_id,
                    "createdAt": created_at,
                }
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as 500, logged, re-raised nowhere
        error(
            "failed to accept ticket",
            ticketId=ticket_id,
            correlationId=correlation_id,
            reason=type(exc).__name__,
        )
        return response(500, {"error": "Could not accept ticket"})

    info("ticket accepted", ticketId=ticket_id, correlationId=correlation_id)
    emit_metric("TicketsSubmitted", 1)

    return response(
        201,
        {"ticketId": ticket_id, "status": STATUS_NEW, "correlationId": correlation_id},
    )


def _read(event: dict[str, Any], requester: str) -> dict[str, Any]:
    """Should-have: the person who raised a ticket can see where it went."""
    ticket_id = (event.get("pathParameters") or {}).get("ticketId")
    if not ticket_id:
        return response(400, {"error": "ticketId is required"})

    try:
        result = _table.get_item(Key={"ticketId": ticket_id})
    except Exception as exc:  # noqa: BLE001
        error("failed to read ticket", ticketId=ticket_id, reason=type(exc).__name__)
        return response(500, {"error": "Could not read ticket"})

    item = result.get("Item")
    # Same 404 whether the ticket is missing or belongs to someone else, so the
    # endpoint cannot be used to probe for valid ticket IDs.
    if not item or item.get("requesterEmail") != requester:
        return response(404, {"error": "Ticket not found"})

    return response(
        200,
        {
            "ticketId": item["ticketId"],
            "status": item.get("status"),
            "category": item.get("category"),
            "priority": item.get("priority"),
            "assignedTeam": item.get("assignedTeam"),
            "createdAt": item.get("createdAt"),
            "routedAt": item.get("routedAt"),
        },
    )
