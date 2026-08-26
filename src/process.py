"""Categorise a ticket with Bedrock, route it, notify the team.

This is where the four custom metrics come from. They are not observability
decoration — each one answers a question the pass criteria cannot:

  TicketsSubmitted (by Category)   which teams are actually carrying load
  CategorisationConfidence         whether the AI should be trusted at all
  TimeToRouteSeconds               how long a person waits before anyone sees it
  ClassificationFallbacks          how often the model's response couldn't be
                                    trusted at all — unparseable or outside the
                                    taxonomy — and routing fell back to the
                                    keyword rule instead
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import boto3

from common import (
    CATEGORIES,
    PRIORITIES,
    STATUS_ROUTED,
    TEAM_BY_CATEGORY,
    emit_metric,
    error,
    info,
    now_iso,
    seconds_between,
    warn,
)

TABLE_NAME = os.environ["TABLE_NAME"]
MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
NOTIFICATION_FROM = os.environ.get("NOTIFICATION_FROM", "")
NOTIFICATION_TO = os.environ.get("NOTIFICATION_TO", "")

# Feature toggle. Lets the Bedrock path merge to main while it is still being
# tuned: flipped off, tickets route to the software team by a keyword rule and
# the flow stays end-to-end testable. This is branching by abstraction — no
# long-lived branch, no merge conflict.
AI_ENABLED = os.environ.get("AI_CATEGORISATION_ENABLED", "true").lower() == "true"

_table = boto3.resource("dynamodb").Table(TABLE_NAME)
_bedrock = boto3.client("bedrock-runtime")
_ses = boto3.client("ses")

PROMPT = """You are triaging an IT helpdesk ticket for a local council.

Classify it into exactly one category and one priority.

Categories:
- network: connectivity, VPN, wifi, DNS, firewall, printers not reachable,
  slow shared drives, phone system
- software: applications, logins, licences, Office, browser errors,
  crashes, permissions inside an application

Priority:
- high: nobody in a team can work, or a service the public relies on is down
- medium: one person blocked, or a team degraded but working
- low: cosmetic, a request, or a question

Reply with JSON only, no prose, no code fences:
{{"category": "...", "priority": "...", "confidence": 0.0}}

confidence is your own certainty in the category, 0.0 to 1.0.

Ticket:
{description}"""

# The prompt above asks for bare JSON, but Haiku 4.5 sometimes wraps its
# answer in a code fence anyway — defence in depth, not a substitute for
# asking. Anchored to the whole string with DOTALL, and greedy on the
# captured body, so this only ever strips a fence that wraps the *entire*
# response: a stray "```" inside a value can't be mistaken for the close,
# because greedy matching backtracks from the end of the string to find the
# real one. `.replace("```", "")` would strip every backtick regardless of
# position and corrupt a value that legitimately contained one.
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*)\s*```$", re.DOTALL)


def _extract_json(text: str) -> str:
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """SQS batch handler. Reports per-message failures so one bad ticket does
    not send a whole batch back to the queue."""
    failures = []

    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            _process_one(json.loads(record["body"]))
        except Exception as exc:  # noqa: BLE001 - retried via SQS, then DLQ
            error("processing failed", messageId=message_id, reason=type(exc).__name__)
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}


def _process_one(message: dict[str, Any]) -> None:
    ticket_id = message["ticketId"]
    correlation_id = message.get("correlationId")

    item = _table.get_item(Key={"ticketId": ticket_id}).get("Item")
    if not item:
        # Nothing to retry: the ticket is gone. Swallow rather than DLQ.
        warn("ticket not found, discarding", ticketId=ticket_id)
        return
    if item.get("status") == STATUS_ROUTED:
        info("already routed, skipping", ticketId=ticket_id)
        return

    category, priority, confidence = _classify(item["description"])
    team = TEAM_BY_CATEGORY[category]
    routed_at = now_iso()

    _table.update_item(
        Key={"ticketId": ticket_id},
        UpdateExpression=(
            "SET #s = :routed, category = :c, priority = :p, "
            "assignedTeam = :t, confidence = :conf, routedAt = :r"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":routed": STATUS_ROUTED,
            ":c": category,
            ":p": priority,
            ":t": team,
            ":conf": str(confidence),
            ":r": routed_at,
        },
    )

    _notify(ticket_id, category, priority, team)

    info(
        "ticket routed",
        ticketId=ticket_id,
        correlationId=correlation_id,
        category=category,
        priority=priority,
        assignedTeam=team,
        confidence=confidence,
    )

    dims = {"Category": category}
    emit_metric("TicketsSubmitted", 1, dimensions=dims)
    emit_metric("CategorisationConfidence", confidence, unit="None", dimensions=dims)

    elapsed = seconds_between(item.get("createdAt", ""), routed_at)
    if elapsed is not None:
        emit_metric("TimeToRouteSeconds", elapsed, unit="Seconds", dimensions=dims)


def _classify(description: str) -> tuple[str, str, float]:
    if not AI_ENABLED:
        return _fallback(description)

    result = _bedrock.converse(
        modelId=MODEL_ID,
        messages=[
            {
                "role": "user",
                "content": [{"text": PROMPT.format(description=description)}],
            }
        ],
        inferenceConfig={"maxTokens": 200, "temperature": 0.0},
    )

    text = result["output"]["message"]["content"][0]["text"].strip()
    try:
        parsed = json.loads(_extract_json(text))
    except json.JSONDecodeError:
        # Same class of problem as an out-of-taxonomy category below: a
        # model response that cannot be trusted. temperature=0.0 makes this
        # deterministic — retrying the identical message via SQS produces
        # the identical unparseable response every time, so this cannot be
        # left to raise and dead-letter; it has to degrade the same way an
        # invalid category does, one check later in this same function.
        warn("model response was not valid JSON, falling back")
        emit_metric("ClassificationFallbacks", 1)
        return _fallback(description)

    category = str(parsed.get("category", "")).lower()
    priority = str(parsed.get("priority", "")).lower()
    confidence = float(parsed.get("confidence", 0.0))

    # A model that returns a category outside the taxonomy is a bug, not a
    # ticket to route badly. Fall back rather than write nonsense to the table.
    if category not in CATEGORIES or priority not in PRIORITIES:
        warn("model returned invalid taxonomy, falling back", returned=category)
        emit_metric("ClassificationFallbacks", 1)
        return _fallback(description)

    return category, priority, max(0.0, min(1.0, confidence))


def _fallback(description: str) -> tuple[str, str, float]:
    """Keyword rule used when the toggle is off or the model misbehaves."""
    network_words = ("wifi", "vpn", "network", "printer", "internet", "connect")
    lowered = description.lower()
    category = "network" if any(w in lowered for w in network_words) else "software"
    return category, "medium", 0.0


def _notify(ticket_id: str, category: str, priority: str, team: str) -> None:
    if not (NOTIFICATION_FROM and NOTIFICATION_TO):
        info("notification skipped: addresses not configured", ticketId=ticket_id)
        return
    try:
        _ses.send_email(
            Source=NOTIFICATION_FROM,
            Destination={"ToAddresses": [NOTIFICATION_TO]},
            Message={
                "Subject": {"Data": f"[{priority.upper()}] Ticket for {team}"},
                # No description, no requester email: the notification says a
                # ticket exists and where to find it, nothing more.
                "Body": {
                    "Text": {
                        "Data": (
                            f"Ticket {ticket_id}\n"
                            f"Category: {category}\n"
                            f"Priority: {priority}\n"
                            f"Assigned to: {team}\n"
                        )
                    }
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        # The ticket is routed and visible in the UI; a failed email is worth an
        # alarm, not a retry that re-routes the ticket.
        error("notification failed", ticketId=ticket_id, reason=type(exc).__name__)
        emit_metric("NotificationFailures", 1)
