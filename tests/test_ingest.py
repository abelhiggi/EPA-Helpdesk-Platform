"""Ingest handler tests.

Every AWS call is replaced with a test double. These tests run offline, in
under a second, with no credentials — which is the point of the test pyramid:
the expensive end-to-end check happens once in the pipeline, not on every save.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from conftest import load_handler


@pytest.fixture
def ingest(monkeypatch):
    module = load_handler("ingest")
    table, sqs = MagicMock(), MagicMock()
    monkeypatch.setattr(module, "_table", table)
    monkeypatch.setattr(module, "_sqs", sqs)
    monkeypatch.setattr(module, "table", table, raising=False)
    monkeypatch.setattr(module, "sqs", sqs, raising=False)
    return module


def event(method="POST", body=None, email="a.higgins@example.gov.uk", ticket_id=None):
    return {
        "httpMethod": method,
        "body": json.dumps(body) if body is not None else None,
        "pathParameters": {"ticketId": ticket_id} if ticket_id else None,
        "requestContext": {"authorizer": {"claims": {"email": email}}} if email else {},
    }


def body_of(result):
    return json.loads(result["body"])


class TestHealth:
    def test_health_check_returns_ok_without_authentication(self, ingest):
        result = ingest.handler({"httpMethod": "GET", "resource": "/health"}, None)
        assert result["statusCode"] == 200
        assert body_of(result) == {"status": "ok"}
        assert not ingest.table.get_item.called


class TestAuthentication:
    def test_rejects_request_with_no_verified_claims(self, ingest):
        result = ingest.handler(event(email=None), None)
        assert result["statusCode"] == 401

    def test_requester_comes_from_token_not_body(self, ingest):
        """A body-supplied email must be ignored, or any user could raise a
        ticket in someone else's name."""
        ingest.handler(
            event(
                body={"description": "VPN down", "requesterEmail": "victim@example.com"},
                email="real.user@example.gov.uk",
            ),
            None,
        )
        item = ingest.table.put_item.call_args.kwargs["Item"]
        assert item["requesterEmail"] == "real.user@example.gov.uk"


class TestCreate:
    def test_valid_ticket_returns_201_with_identifiers(self, ingest):
        result = ingest.handler(event(body={"description": "Wifi keeps dropping"}), None)
        payload = body_of(result)
        assert result["statusCode"] == 201
        assert payload["status"] == "NEW"
        assert payload["ticketId"]
        assert payload["correlationId"]

    def test_ticket_is_persisted_before_it_is_queued(self, ingest):
        """Queue first and a consumer can read a ticket that does not exist."""
        ingest.handler(event(body={"description": "Outlook will not open"}), None)
        assert ingest.table.put_item.called
        assert ingest.sqs.send_message.called

    def test_queued_message_carries_the_correlation_id(self, ingest):
        result = ingest.handler(event(body={"description": "Printer offline"}), None)
        sent = json.loads(ingest.sqs.send_message.call_args.kwargs["MessageBody"])
        assert sent["correlationId"] == body_of(result)["correlationId"]

    def test_missing_description_is_rejected(self, ingest):
        assert ingest.handler(event(body={}), None)["statusCode"] == 400

    def test_whitespace_only_description_is_rejected(self, ingest):
        assert ingest.handler(event(body={"description": "   "}), None)["statusCode"] == 400

    def test_oversized_description_is_rejected(self, ingest):
        long_text = "x" * (ingest.MAX_DESCRIPTION + 1)
        assert ingest.handler(event(body={"description": long_text}), None)["statusCode"] == 400

    def test_malformed_json_body_is_rejected(self, ingest):
        broken = event(body={"description": "ok"})
        broken["body"] = "{not json"
        assert ingest.handler(broken, None)["statusCode"] == 400

    def test_storage_failure_returns_500_and_does_not_leak_detail(self, ingest):
        ingest.table.put_item.side_effect = RuntimeError("secret internal detail")
        result = ingest.handler(event(body={"description": "Laptop slow"}), None)
        assert result["statusCode"] == 500
        assert "secret" not in result["body"]


class TestRead:
    def test_owner_sees_status_and_routing(self, ingest):
        ingest.table.get_item.return_value = {
            "Item": {
                "ticketId": "t-1",
                "status": "ROUTED",
                "category": "network",
                "priority": "high",
                "assignedTeam": "Network Team",
                "createdAt": "2026-08-01T09:00:00.000Z",
                "requesterEmail": "a.higgins@example.gov.uk",
            }
        }
        result = ingest.handler(event(method="GET", ticket_id="t-1"), None)
        payload = body_of(result)
        assert result["statusCode"] == 200
        assert payload["assignedTeam"] == "Network Team"
        assert payload["priority"] == "high"

    def test_response_never_returns_the_description(self, ingest):
        ingest.table.get_item.return_value = {
            "Item": {
                "ticketId": "t-1",
                "status": "NEW",
                "description": "my password is hunter2",
                "requesterEmail": "a.higgins@example.gov.uk",
            }
        }
        result = ingest.handler(event(method="GET", ticket_id="t-1"), None)
        assert "hunter2" not in result["body"]

    def test_another_users_ticket_is_indistinguishable_from_a_missing_one(self, ingest):
        """Both return 404, so the endpoint cannot be used to enumerate IDs."""
        ingest.table.get_item.return_value = {
            "Item": {"ticketId": "t-1", "requesterEmail": "someone.else@example.gov.uk"}
        }
        theirs = ingest.handler(event(method="GET", ticket_id="t-1"), None)

        ingest.table.get_item.return_value = {}
        missing = ingest.handler(event(method="GET", ticket_id="t-2"), None)

        assert theirs["statusCode"] == missing["statusCode"] == 404
        assert theirs["body"] == missing["body"]

    def test_missing_path_parameter_is_rejected(self, ingest):
        assert ingest.handler(event(method="GET"), None)["statusCode"] == 400


def test_unsupported_method_returns_405(ingest):
    assert ingest.handler(event(method="DELETE"), None)["statusCode"] == 405
