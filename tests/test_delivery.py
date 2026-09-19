"""Sending a report once, and knowing when you cannot be sure you did.

The failures worth testing here are not "did the email go out" but the ones that put the same board
pack in someone's inbox twice, or silently put it in nobody's: a rerun of an unchanged edition, a
provider that times out after accepting the request, a process that dies mid-send, and a dry run
that quietly consumes the delivery it was only supposed to rehearse.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from azmonitor.delivery import providers as P
from azmonitor.delivery.records import AMBIGUOUS, DeliveryLedger, delivery_id, mask

SEND = dict(report_type="monthly", edition="2026-07", version=36, channel="email", recipient_id="owner",
            recipient_address="owner@example.invalid", provider="microsoft_graph", subject="Monitor 2026-07",
            content_hash="abc123")


@pytest.fixture()
def ledger(tmp_path) -> DeliveryLedger:
    led = DeliveryLedger(tmp_path / "deliveries.sqlite")
    yield led
    led.close()


def test_the_same_edition_is_not_sent_to_the_same_person_twice(ledger):
    did, state = ledger.enqueue(**SEND)
    assert state == "new"
    ledger.mark_sending(did)
    ledger.mark_sent(did, "graph-accepted-202")

    # the rerun a scheduler performs every few hours
    again_id, again_state = ledger.enqueue(**SEND)
    assert again_id == did and again_state == "sent"
    assert ledger.summary() == {"sent": 1}


def test_a_new_version_of_the_same_edition_is_a_new_delivery(ledger):
    """A corrected edition should reach people; only a repeat of the same one should not."""
    first, _ = ledger.enqueue(**SEND)
    ledger.mark_sending(first)
    ledger.mark_sent(first, "m1")

    second, state = ledger.enqueue(**{**SEND, "version": 37, "content_hash": "def456"})
    assert second != first and state == "new"


def test_an_ambiguous_response_is_never_retried_automatically(ledger):
    """A timeout after the request was accepted. Retrying is how a board gets it twice."""
    did, _ = ledger.enqueue(**SEND)
    ledger.mark_sending(did)
    ledger.mark_needs_review(did, "connection lost while sending")

    assert [r["delivery_id"] for r in ledger.due()] == []          # not picked up again
    assert [r["delivery_id"] for r in ledger.needing_review()] == [did]
    assert ledger.enqueue(**SEND)[1] == AMBIGUOUS                  # and a rerun does not resend


def test_a_person_resolves_an_ambiguous_delivery_either_way(ledger):
    did, _ = ledger.enqueue(**SEND)
    ledger.mark_sending(did)
    ledger.mark_needs_review(did, "no answer from the provider")

    assert ledger.resolve(did, "delivered", "t.hasanov") is True
    assert ledger.get(did)["status"] == "sent"
    assert ledger.get(did)["resolved_by"] == "t.hasanov"

    other, _ = ledger.enqueue(**{**SEND, "recipient_id": "cro", "recipient_address": "cro@example.invalid"})
    ledger.mark_sending(other)
    ledger.mark_needs_review(other, "no answer")
    assert ledger.resolve(other, "not_delivered", "t.hasanov") is True
    assert ledger.get(other)["status"] == "pending"               # back in the queue, deliberately
    assert [r["delivery_id"] for r in ledger.due()] == [other]

    with pytest.raises(ValueError):
        ledger.resolve(did, "probably", "t.hasanov")


def test_a_run_that_dies_mid_send_leaves_the_delivery_for_a_person(ledger):
    """The row says `sending` and nothing else is knowable: it is ambiguous, not failed."""
    did, _ = ledger.enqueue(**SEND)
    ledger.mark_sending(did)
    ledger.conn.execute("UPDATE deliveries SET updated_at=? WHERE delivery_id=?",
                        ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=4)).isoformat(), did))
    ledger.conn.commit()

    assert ledger.recover_stuck(older_than_minutes=120) == [did]
    assert ledger.get(did)["status"] == AMBIGUOUS
    assert "never established" in ledger.get(did)["last_error"]


def test_a_dry_run_does_not_consume_the_delivery_it_rehearsed(ledger):
    """A skip records the intent; the send is still owed once the channel is switched on."""
    did, _ = ledger.enqueue(**SEND)
    ledger.mark_skipped(did, "dry run")

    same, state = ledger.enqueue(**SEND)
    assert same == did and state == "retry_after_skip"
    assert ledger.get(did)["status"] == "pending"


def test_a_clear_refusal_is_retried_within_its_backoff(ledger):
    did, _ = ledger.enqueue(**SEND)
    ledger.mark_sending(did)
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
    ledger.mark_failed(did, "HTTP 429", future)

    assert [r["delivery_id"] for r in ledger.due()] == []          # not yet
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()
    ledger.conn.execute("UPDATE deliveries SET next_attempt_at=? WHERE delivery_id=?", (past, did))
    ledger.conn.commit()
    assert [r["delivery_id"] for r in ledger.due()] == [did]       # now


def test_an_address_is_recorded_only_as_a_hint(ledger):
    """The ledger identifies a recipient by id; the address is masked so it is not a contact list."""
    did, _ = ledger.enqueue(**SEND)
    hint = ledger.get(did)["recipient_hint"]
    assert "owner@example.invalid" not in hint and hint.endswith("@example.invalid")
    assert mask("+994501234567").endswith("4567") and "9945" not in mask("+994501234567")
    assert mask(None) == ""


def test_delivery_identity_ignores_everything_except_who_gets_what(ledger):
    a = delivery_id("monthly", "2026-07", 36, "email", "owner")
    assert a == delivery_id("monthly", "2026-07", 36, "email", "owner")
    assert a != delivery_id("monthly", "2026-07", 36, "whatsapp", "owner")
    assert a != delivery_id("monthly", "2026-08", 36, "email", "owner")
    assert a != delivery_id("weekly", "2026-07", 36, "email", "owner")


# ------------------------------------------------------------------ providers

class _FakeHTTPError(Exception):
    def __init__(self, code: int):
        self.code = code

    def read(self):
        return b'{"error":"x"}'


def test_a_five_hundred_after_the_request_was_accepted_is_ambiguous(monkeypatch):
    """The distinction the whole design rests on: refused is retryable, unanswered is not."""
    import urllib.error

    cfg = {"provider": "microsoft_graph", "sender_env": "T_SENDER",
           "microsoft_graph": {"tenant_id_env": "T_TEN", "client_id_env": "T_CID",
                               "client_secret_env": "T_SEC", "timeout_seconds": 5}}
    for k, v in {"T_SENDER": "s@example.invalid", "T_TEN": "t", "T_CID": "c", "T_SEC": "s"}.items():
        monkeypatch.setenv(k, v)
    graph = P.MicrosoftGraph(cfg)
    monkeypatch.setattr(graph, "_access_token", lambda: "token")

    def raise_503(*a, **k):
        raise urllib.error.HTTPError("u", 503, "boom", {}, None)
    monkeypatch.setattr(P, "_post", raise_503)
    assert graph.send(to="a@b.invalid", subject="s", body_text="t", body_html="<p>t</p>").outcome == "ambiguous"

    def raise_400(*a, **k):
        raise urllib.error.HTTPError("u", 400, "bad", {}, None)
    monkeypatch.setattr(P, "_post", raise_400)
    assert graph.send(to="a@b.invalid", subject="s", body_text="t", body_html="<p>t</p>").outcome == "failed"

    def timeout(*a, **k):
        raise TimeoutError("read timed out")
    monkeypatch.setattr(P, "_post", timeout)
    res = graph.send(to="a@b.invalid", subject="s", body_text="t", body_html="<p>t</p>")
    assert res.outcome == "ambiguous" and "connection lost" in res.error

    monkeypatch.setattr(P, "_post", lambda *a, **k: (202, ""))
    assert graph.send(to="a@b.invalid", subject="s", body_text="t", body_html="<p>t</p>").outcome == "sent"


def test_a_missing_credential_names_the_variable_not_the_value(monkeypatch):
    monkeypatch.delenv("T_SEC", raising=False)
    cfg = {"provider": "microsoft_graph", "sender_env": "T_SENDER",
           "microsoft_graph": {"tenant_id_env": "T_TEN", "client_id_env": "T_CID", "client_secret_env": "T_SEC"}}
    monkeypatch.setenv("T_SENDER", "s@example.invalid")
    monkeypatch.setenv("T_TEN", "t")
    monkeypatch.setenv("T_CID", "c")
    graph = P.MicrosoftGraph(cfg)
    assert graph.missing_credentials() == ["T_SEC"]
    res = graph.send(to="a@b.invalid", subject="s", body_text="t", body_html="<p>t</p>")
    assert res.outcome == "failed" and "T_SEC" in res.error


def test_whatsapp_success_without_a_message_id_is_ambiguous(monkeypatch):
    cfg = {"provider": "meta_cloud_api",
           "meta_cloud_api": {"phone_number_id_env": "T_PN", "access_token_env": "T_TOK"},
           "templates": {"report_ready": {"name": "azmonitor_report_ready", "language": "en",
                                          "variables": ["report_name", "edition", "location_hint"]}}}
    monkeypatch.setenv("T_PN", "1")
    monkeypatch.setenv("T_TOK", "x")
    wa = P.MetaWhatsApp(cfg)
    vars_ = {"report_name": "Monitor", "edition": "2026-07", "location_hint": "the archive"}

    monkeypatch.setattr(P, "_post", lambda *a, **k: (200, json.dumps({"messages": [{"id": "wamid.1"}]})))
    assert wa.send_template(to="+1", template_key="report_ready", variables=vars_).outcome == "sent"

    monkeypatch.setattr(P, "_post", lambda *a, **k: (200, json.dumps({"messages": []})))
    res = wa.send_template(to="+1", template_key="report_ready", variables=vars_)
    assert res.outcome == "ambiguous" and "without a message id" in res.error

    # a template variable the approved template needs but the caller did not supply
    res = wa.send_template(to="+1", template_key="report_ready", variables={"report_name": "Monitor"})
    assert res.outcome == "failed" and "edition" in res.error
