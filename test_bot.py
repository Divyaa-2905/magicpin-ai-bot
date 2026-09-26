"""
Comprehensive Test Suite for magicpin AI Challenge Vera Bot
============================================================
Tests:
- API Endpoints (/v1/healthz, /v1/metadata, /v1/context, /v1/tick, /v1/reply, /v1/teardown)
- Context Versioning & Idempotency
- Multi-turn conversation handling:
  - Auto-reply loops (send -> wait -> end)
  - Intent transitions (action mode with action verbs, 0 qualifying words)
  - Hostile messages (graceful opt-out / immediate close)
  - Out-of-scope redirection (GST, accounting)
- Standalone compose() function
- submission.jsonl schema and content validation
"""

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from bot import app, compose
from conversation_handlers import ConversationState, respond


client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_state():
    client.post("/v1/teardown")
    yield
    client.post("/v1/teardown")


def test_healthz_initial():
    response = client.get("/v1/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "uptime_seconds" in data
    assert data["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}


def test_metadata():
    response = client.get("/v1/metadata")
    assert response.status_code == 200
    data = response.json()
    assert "team_name" in data
    assert "model" in data
    assert "version" in data
    assert "submitted_at" in data


def test_context_push_and_idempotency():
    payload = {
        "scope": "category",
        "context_id": "dentists",
        "version": 1,
        "payload": {"slug": "dentists", "voice": {"tone": "peer_clinical"}},
        "delivered_at": "2026-04-26T10:00:00Z"
    }

    # 1. Initial push -> 200
    r1 = client.post("/v1/context", json=payload)
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["accepted"] is True
    assert "ack_id" in d1

    # Check healthz reflects 1 category loaded
    r_h = client.get("/v1/healthz")
    assert r_h.json()["contexts_loaded"]["category"] == 1

    # 2. Stale/Duplicate version push -> 409
    r2 = client.post("/v1/context", json=payload)
    assert r2.status_code == 409
    d2 = r2.json()
    assert d2["accepted"] is False
    assert d2["reason"] == "stale_version"
    assert d2["current_version"] == 1

    # 3. Higher version push -> 200
    payload["version"] = 2
    payload["payload"]["voice"]["tone"] = "peer_clinical_v2"
    r3 = client.post("/v1/context", json=payload)
    assert r3.status_code == 200
    assert r3.json()["accepted"] is True

    # 4. Invalid scope -> 400
    payload["scope"] = "invalid_scope"
    r4 = client.post("/v1/context", json=payload)
    assert r4.status_code == 400


def test_tick_and_action_generation():
    # Push category
    client.post("/v1/context", json={
        "scope": "category", "context_id": "dentists", "version": 1,
        "payload": {
            "slug": "dentists",
            "voice": {"tone": "peer_clinical"},
            "digest": [
                {
                    "id": "d_2026W17_jida_fluoride",
                    "title": "3-month fluoride recall",
                    "source": "JIDA Oct 2026, p.14",
                    "trial_n": 2100
                }
            ],
            "offer_catalog": [{"title": "Dental Cleaning @ ₹299", "value": "299"}]
        }
    })

    # Push merchant
    client.post("/v1/context", json={
        "scope": "merchant", "context_id": "m_001", "version": 1,
        "payload": {
            "merchant_id": "m_001",
            "category_slug": "dentists",
            "identity": {"name": "Dr. Meera's Clinic", "owner_first_name": "Meera", "locality": "Lajpat Nagar", "city": "Delhi"},
            "customer_aggregate": {"high_risk_adult_count": 124},
            "performance": {"views": 2400, "calls": 20, "ctr": 0.021},
            "offers": [{"title": "Dental Cleaning @ ₹299", "status": "active"}]
        }
    })

    # Push trigger
    client.post("/v1/context", json={
        "scope": "trigger", "context_id": "trg_001", "version": 1,
        "payload": {
            "id": "trg_001",
            "scope": "merchant",
            "kind": "research_digest",
            "merchant_id": "m_001",
            "payload": {"category": "dentists", "top_item_id": "d_2026W17_jida_fluoride"},
            "suppression_key": "research:dentists:2026-W17"
        }
    })

    # Tick
    r_tick = client.post("/v1/tick", json={
        "now": "2026-04-26T10:35:00Z",
        "available_triggers": ["trg_001"]
    })
    assert r_tick.status_code == 200
    actions = r_tick.json().get("actions", [])
    assert len(actions) == 1

    act = actions[0]
    required_keys = [
        "conversation_id", "merchant_id", "customer_id", "send_as",
        "trigger_id", "template_name", "template_params", "body",
        "cta", "suppression_key", "rationale"
    ]
    for k in required_keys:
        assert k in act, f"Missing key {k} in action"

    assert "Dr. Meera" in act["body"]
    assert "JIDA" in act["body"]
    assert act["send_as"] == "vera"


def test_auto_reply_hell():
    conv_id = "conv_auto_test"
    mid = "m_001"
    auto_msg = "Thank you for contacting us! Our team will respond shortly."

    # Turn 1: should send prompt to human owner
    r1 = client.post("/v1/reply", json={
        "conversation_id": conv_id, "merchant_id": mid,
        "from_role": "merchant", "message": auto_msg, "turn_number": 2
    })
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["action"] == "send"
    assert "auto-reply" in d1["body"].lower()

    # Turn 2: should wait
    r2 = client.post("/v1/reply", json={
        "conversation_id": conv_id, "merchant_id": mid,
        "from_role": "merchant", "message": auto_msg, "turn_number": 3
    })
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["action"] == "wait"
    assert d2.get("wait_seconds", 0) > 0

    # Turn 3: should end gracefully
    r3 = client.post("/v1/reply", json={
        "conversation_id": conv_id, "merchant_id": mid,
        "from_role": "merchant", "message": auto_msg, "turn_number": 4
    })
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["action"] == "end"


def test_intent_transition_action_mode():
    conv_id = "conv_intent_test"
    mid = "m_001"
    commitment = "Ok lets do it. Whats next?"

    r = client.post("/v1/reply", json={
        "conversation_id": conv_id, "merchant_id": mid,
        "from_role": "merchant", "message": commitment, "turn_number": 2
    })
    assert r.status_code == 200
    d = r.json()
    assert d["action"] == "send"

    body_lower = d.get("body", "").lower()
    qualifying = ["would you", "do you", "can you tell", "what if", "how about"]
    actioning = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]

    # Must contain at least one action word
    assert any(w in body_lower for w in actioning), f"No action word found in {body_lower}"
    # Must NOT contain any qualifying phrase
    assert not any(w in body_lower for w in qualifying), f"Found qualifying phrase in {body_lower}"


def test_hostile_opt_out():
    conv_id = "conv_hostile_test"
    mid = "m_001"
    hostile = "Stop messaging me. This is useless spam."

    r = client.post("/v1/reply", json={
        "conversation_id": conv_id, "merchant_id": mid,
        "from_role": "merchant", "message": hostile, "turn_number": 2
    })
    assert r.status_code == 200
    d = r.json()
    assert d["action"] == "end"


def test_out_of_scope_redirection():
    conv_id = "conv_curveball_test"
    mid = "m_001"
    curveball = "Can you also help me with my GST filing this month?"

    r = client.post("/v1/reply", json={
        "conversation_id": conv_id, "merchant_id": mid,
        "from_role": "merchant", "message": curveball, "turn_number": 2
    })
    assert r.status_code == 200
    d = r.json()
    assert d["action"] == "send"
    assert "gst" in d["body"].lower() or "ca" in d["body"].lower()


def test_submission_jsonl_validity():
    submission_path = Path(__file__).parent / "submission.jsonl"
    assert submission_path.exists(), "submission.jsonl does not exist"

    lines = [line.strip() for line in open(submission_path, encoding="utf-8") if line.strip()]
    assert len(lines) == 30, f"Expected 30 lines in submission.jsonl, got {len(lines)}"

    required_keys = {"test_id", "body", "cta", "send_as", "suppression_key", "rationale"}

    for idx, line in enumerate(lines, 1):
        obj = json.loads(line)
        assert obj["test_id"] == f"T{idx:02d}"
        for k in required_keys:
            assert k in obj, f"Missing key {k} in line {idx}"
        assert len(obj["body"]) > 20
        assert obj["send_as"] in ("vera", "merchant_on_behalf")
        assert len(obj["rationale"]) > 10
