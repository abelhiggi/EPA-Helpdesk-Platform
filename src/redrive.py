"""Automated DLQ recovery.

The additional automation opportunity: before this existed, a message in the
DLQ meant someone noticing an alarm email, opening the console, reading the
message, deciding it was a transient failure and hand-copying it back to the
source queue. Measured at roughly ten to fifteen minutes per incident, and only
during working hours.

Triggered by the DLQ depth alarm. Bounded on purpose — it drains up to
MAX_DRAIN messages and re-queues each one at most MAX_ATTEMPTS times, so a
genuinely poisonous message parks itself instead of looping forever.
"""

from __future__ import annotations

import os
from typing import Any

import boto3

from common import emit_metric, info, warn

DLQ_URL = os.environ["DLQ_URL"]
QUEUE_URL = os.environ["QUEUE_URL"]
MAX_DRAIN = 20
MAX_ATTEMPTS = 2

_sqs = boto3.client("sqs")


def handler(_event: dict[str, Any], _context: Any) -> dict[str, Any]:
    requeued = 0
    abandoned = 0

    while requeued + abandoned < MAX_DRAIN:
        batch = _sqs.receive_message(
            QueueUrl=DLQ_URL,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=1,
            MessageAttributeNames=["All"],
        ).get("Messages", [])

        if not batch:
            break

        for message in batch:
            attempts = _attempt_count(message)

            if attempts >= MAX_ATTEMPTS:
                # Left in the DLQ deliberately. A message that has failed twice
                # after redrive needs a human, and the alarm keeps firing until
                # one arrives.
                warn("abandoning message after repeated redrive", attempts=attempts)
                abandoned += 1
                continue

            _sqs.send_message(
                QueueUrl=QUEUE_URL,
                MessageBody=message["Body"],
                MessageAttributes={
                    "redriveAttempt": {
                        "DataType": "Number",
                        "StringValue": str(attempts + 1),
                    }
                },
            )
            # Delete only after the send succeeds. The other order loses
            # messages when the send fails.
            _sqs.delete_message(QueueUrl=DLQ_URL, ReceiptHandle=message["ReceiptHandle"])
            requeued += 1

    info("redrive complete", requeued=requeued, abandoned=abandoned)
    emit_metric("MessagesRedriven", requeued)
    emit_metric("MessagesAbandoned", abandoned)

    return {"requeued": requeued, "abandoned": abandoned}


def _attempt_count(message: dict[str, Any]) -> int:
    attributes = message.get("MessageAttributes") or {}
    raw = (attributes.get("redriveAttempt") or {}).get("StringValue")
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0
