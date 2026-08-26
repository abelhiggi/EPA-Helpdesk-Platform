"""Redrive tests.

The two behaviours that matter: a message is never lost, and a poison message
never loops forever.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from conftest import load_handler


@pytest.fixture
def redrive(monkeypatch):
    module = load_handler("redrive")
    sqs = MagicMock()
    monkeypatch.setattr(module, "_sqs", sqs)
    monkeypatch.setattr(module, "sqs", sqs, raising=False)
    return module


def message(body="{}", attempt=None, receipt="r-1"):
    msg = {"Body": body, "ReceiptHandle": receipt}
    if attempt is not None:
        msg["MessageAttributes"] = {
            "redriveAttempt": {"DataType": "Number", "StringValue": str(attempt)}
        }
    return msg


def batches(*rounds):
    """Successive receive_message results, then an empty poll to stop the loop."""
    return [{"Messages": list(r)} for r in rounds] + [{"Messages": []}]


def test_empty_dlq_does_no_work(redrive):
    redrive.sqs.receive_message.return_value = {"Messages": []}
    result = redrive.handler({}, None)
    assert result == {"requeued": 0, "abandoned": 0}
    assert not redrive.sqs.send_message.called


def test_first_time_failure_is_requeued(redrive):
    redrive.sqs.receive_message.side_effect = batches([message(body='{"ticketId":"t-1"}')])
    result = redrive.handler({}, None)
    assert result["requeued"] == 1
    assert redrive.sqs.send_message.call_args.kwargs["MessageBody"] == '{"ticketId":"t-1"}'


def test_requeued_message_carries_an_incremented_attempt_count(redrive):
    redrive.sqs.receive_message.side_effect = batches([message(attempt=1)])
    redrive.handler({}, None)
    attributes = redrive.sqs.send_message.call_args.kwargs["MessageAttributes"]
    assert attributes["redriveAttempt"]["StringValue"] == "2"


def test_message_is_deleted_only_after_a_successful_send(redrive):
    """Deleting first would lose the message whenever the send fails."""
    redrive.sqs.receive_message.side_effect = batches([message()])
    redrive.sqs.send_message.side_effect = RuntimeError("send failed")

    with pytest.raises(RuntimeError):
        redrive.handler({}, None)

    assert not redrive.sqs.delete_message.called


def test_poison_message_is_abandoned_rather_than_looped(redrive):
    redrive.sqs.receive_message.side_effect = batches([message(attempt=redrive.MAX_ATTEMPTS)])
    result = redrive.handler({}, None)
    assert result == {"requeued": 0, "abandoned": 1}
    assert not redrive.sqs.send_message.called
    assert not redrive.sqs.delete_message.called


def test_poison_message_does_not_block_a_recoverable_one(redrive):
    redrive.sqs.receive_message.side_effect = batches(
        [message(attempt=redrive.MAX_ATTEMPTS, receipt="poison"), message(receipt="fresh")]
    )
    result = redrive.handler({}, None)
    assert result == {"requeued": 1, "abandoned": 1}


def test_drain_is_bounded(redrive):
    """An unbounded loop on a large DLQ would run until the timeout and then
    redrive the same messages again on the next alarm."""
    redrive.sqs.receive_message.return_value = {
        "Messages": [message(receipt=f"r-{i}") for i in range(10)]
    }
    result = redrive.handler({}, None)
    assert result["requeued"] + result["abandoned"] <= redrive.MAX_DRAIN


def test_unparseable_attempt_attribute_is_treated_as_a_first_attempt(redrive):
    msg = message()
    msg["MessageAttributes"] = {
        "redriveAttempt": {"DataType": "Number", "StringValue": "not-a-number"}
    }
    redrive.sqs.receive_message.side_effect = batches([msg])
    assert redrive.handler({}, None)["requeued"] == 1
