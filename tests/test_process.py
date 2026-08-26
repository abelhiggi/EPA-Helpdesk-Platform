"""Process handler tests.

Bedrock is stubbed rather than called. Real model calls would make the suite
slow, non-deterministic and billable — and the thing under test here is how the
handler responds to what the model returns, not the model itself.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from conftest import load_handler


def bedrock_returning(payload):
    """Test double shaped like a Bedrock Converse response."""
    client = MagicMock()
    text = payload if isinstance(payload, str) else json.dumps(payload)
    client.converse.return_value = {"output": {"message": {"content": [{"text": text}]}}}
    return client


@pytest.fixture
def process(monkeypatch):
    process_module = load_handler("process")

    table, ses = MagicMock(), MagicMock()
    monkeypatch.setattr(process_module, "_table", table)
    monkeypatch.setattr(process_module, "_ses", ses)
    monkeypatch.setattr(process_module, "AI_ENABLED", True)
    monkeypatch.setattr(process_module, "table", table, raising=False)
    monkeypatch.setattr(process_module, "ses", ses, raising=False)
    table.get_item.return_value = {
        "Item": {
            "ticketId": "t-1",
            "status": "NEW",
            "description": "The wifi keeps dropping in the north wing",
            "createdAt": "2026-08-01T09:00:00.000Z",
        }
    }
    return process_module


def sqs_event(ticket_id="t-1", message_id="m-1"):
    return {
        "Records": [
            {
                "messageId": message_id,
                "body": json.dumps({"ticketId": ticket_id, "correlationId": "c-1"}),
            }
        ]
    }


def written(process):
    return process.table.update_item.call_args.kwargs["ExpressionAttributeValues"]


class TestCategorisation:
    def test_model_result_is_written_to_the_ticket(self, process, monkeypatch):
        monkeypatch.setattr(
            process,
            "_bedrock",
            bedrock_returning({"category": "network", "priority": "high", "confidence": 0.93}),
        )
        process.handler(sqs_event(), None)
        values = written(process)
        assert values[":c"] == "network"
        assert values[":p"] == "high"
        assert values[":t"] == "Network Team"
        assert values[":routed"] == "ROUTED"

    def test_category_outside_the_taxonomy_falls_back_instead_of_writing_nonsense(
        self, process, monkeypatch
    ):
        monkeypatch.setattr(
            process,
            "_bedrock",
            bedrock_returning({"category": "hardware", "priority": "high", "confidence": 0.9}),
        )
        process.handler(sqs_event(), None)
        assert written(process)[":c"] in process.CATEGORIES

    def test_invalid_priority_falls_back(self, process, monkeypatch):
        monkeypatch.setattr(
            process,
            "_bedrock",
            bedrock_returning({"category": "network", "priority": "urgent", "confidence": 0.9}),
        )
        process.handler(sqs_event(), None)
        assert written(process)[":p"] in process.PRIORITIES

    def test_confidence_is_clamped_to_a_valid_range(self, process, monkeypatch):
        monkeypatch.setattr(
            process,
            "_bedrock",
            bedrock_returning({"category": "network", "priority": "low", "confidence": 4.2}),
        )
        process.handler(sqs_event(), None)
        assert float(written(process)[":conf"]) <= 1.0

    def test_unparseable_model_output_falls_back_instead_of_raising(self, process, monkeypatch):
        """Previously this asserted the message became a batchItemFailure and
        was retried via SQS. That was wrong in production: temperature=0.0
        makes the model deterministic, so a retry gets back the identical
        unparseable response every time, not a transient glitch that
        clears. That means it isn't a retryable failure at all — it's the
        same class of problem as an out-of-taxonomy category one check
        later in _classify, which already degrades gracefully to the
        keyword fallback rather than raising. An unparseable response
        belongs on that same path, so the ticket still routes rather than
        dead-lettering and parking permanently."""
        monkeypatch.setattr(process, "_bedrock", bedrock_returning("I'd be happy to help!"))
        result = process.handler(sqs_event(), None)
        assert result["batchItemFailures"] == []
        assert written(process)[":routed"] == "ROUTED"

    def test_fenced_json_response_with_language_tag_parses_correctly(self, process, monkeypatch):
        """Haiku 4.5 wraps its answer in a ```json fence despite the prompt
        saying not to — this is the exact response observed in the incident
        this test guards against (see docs/troubleshooting.md)."""
        monkeypatch.setattr(
            process,
            "_bedrock",
            bedrock_returning(
                '```json\n{"category": "network", "priority": "high", "confidence": 0.95}\n```'
            ),
        )
        process.handler(sqs_event(), None)
        values = written(process)
        assert values[":c"] == "network"
        assert values[":p"] == "high"
        assert values[":routed"] == "ROUTED"

    def test_bare_fenced_json_response_parses_correctly(self, process, monkeypatch):
        monkeypatch.setattr(
            process,
            "_bedrock",
            bedrock_returning(
                '```\n{"category": "software", "priority": "low", "confidence": 0.5}\n```'
            ),
        )
        process.handler(sqs_event(), None)
        values = written(process)
        assert values[":c"] == "software"
        assert values[":p"] == "low"
        assert values[":routed"] == "ROUTED"

    def test_fenced_response_with_out_of_taxonomy_category_still_falls_back(
        self, process, monkeypatch
    ):
        monkeypatch.setattr(
            process,
            "_bedrock",
            bedrock_returning(
                '```json\n{"category": "hardware", "priority": "high", "confidence": 0.9}\n```'
            ),
        )
        process.handler(sqs_event(), None)
        values = written(process)
        assert values[":c"] in process.CATEGORIES
        assert values[":routed"] == "ROUTED"

    def test_fallback_emits_a_classification_fallbacks_metric(self, process, monkeypatch):
        metric = MagicMock()
        monkeypatch.setattr(process, "emit_metric", metric)
        monkeypatch.setattr(process, "_bedrock", bedrock_returning("not json at all"))
        process.handler(sqs_event(), None)
        metric.assert_any_call("ClassificationFallbacks", 1)

    def test_successful_classification_does_not_emit_a_fallback_metric(self, process, monkeypatch):
        metric = MagicMock()
        monkeypatch.setattr(process, "emit_metric", metric)
        monkeypatch.setattr(
            process,
            "_bedrock",
            bedrock_returning({"category": "network", "priority": "high", "confidence": 0.9}),
        )
        process.handler(sqs_event(), None)
        assert "ClassificationFallbacks" not in [call.args[0] for call in metric.call_args_list]


class TestFeatureToggle:
    def test_toggle_off_routes_without_calling_bedrock(self, process, monkeypatch):
        """Branching by abstraction: the AI path can merge to main switched off
        and the flow stays end-to-end testable."""
        bedrock = bedrock_returning({"category": "network", "priority": "low", "confidence": 1.0})
        monkeypatch.setattr(process, "_bedrock", bedrock)
        monkeypatch.setattr(process, "AI_ENABLED", False)

        process.handler(sqs_event(), None)

        assert not bedrock.converse.called
        assert written(process)[":routed"] == "ROUTED"

    def test_fallback_keyword_rule_still_classifies_sensibly(self, process, monkeypatch):
        monkeypatch.setattr(process, "AI_ENABLED", False)
        process.handler(sqs_event(), None)
        assert written(process)[":c"] == "network"


class TestIdempotencyAndFailure:
    def test_already_routed_ticket_is_skipped(self, process, monkeypatch):
        """SQS is at-least-once, so redelivery must not re-notify the team."""
        monkeypatch.setattr(
            process,
            "_bedrock",
            bedrock_returning({"category": "network", "priority": "low", "confidence": 1.0}),
        )
        process.table.get_item.return_value = {
            "Item": {"ticketId": "t-1", "status": "ROUTED", "description": "x"}
        }
        process.handler(sqs_event(), None)
        assert not process.table.update_item.called
        assert not process.ses.send_email.called

    def test_missing_ticket_is_discarded_not_retried(self, process):
        process.table.get_item.return_value = {}
        result = process.handler(sqs_event(), None)
        assert result["batchItemFailures"] == []

    def test_one_bad_message_does_not_fail_the_whole_batch(self, process, monkeypatch):
        monkeypatch.setattr(
            process,
            "_bedrock",
            bedrock_returning({"category": "network", "priority": "low", "confidence": 1.0}),
        )
        event = sqs_event()
        event["Records"].append({"messageId": "m-2", "body": "{broken"})

        result = process.handler(event, None)

        assert result["batchItemFailures"] == [{"itemIdentifier": "m-2"}]

    def test_notification_failure_does_not_unroute_the_ticket(self, process, monkeypatch):
        monkeypatch.setattr(
            process,
            "_bedrock",
            bedrock_returning({"category": "software", "priority": "medium", "confidence": 0.8}),
        )
        process.ses.send_email.side_effect = RuntimeError("SES unavailable")
        result = process.handler(sqs_event(), None)
        assert result["batchItemFailures"] == []
        assert written(process)[":routed"] == "ROUTED"


class TestNotificationContent:
    def test_email_contains_no_description_or_requester(self, process, monkeypatch):
        monkeypatch.setattr(
            process,
            "_bedrock",
            bedrock_returning({"category": "network", "priority": "high", "confidence": 0.9}),
        )
        process.table.get_item.return_value = {
            "Item": {
                "ticketId": "t-1",
                "status": "NEW",
                "description": "my password is hunter2",
                "requesterEmail": "a.higgins@example.gov.uk",
                "createdAt": "2026-08-01T09:00:00.000Z",
            }
        }
        process.handler(sqs_event(), None)
        sent = json.dumps(process.ses.send_email.call_args.kwargs)
        assert "hunter2" not in sent
        assert "a.higgins" not in sent
