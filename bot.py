"""
magicpin AI Challenge — Vera Bot HTTP Service & Public API
===========================================================
Production-ready FastAPI service exposing the 5 required judging endpoints:
- POST /v1/context
- POST /v1/tick
- POST /v1/reply
- GET /v1/healthz
- GET /v1/metadata
- POST /v1/teardown (optional cleanup)

Also exports the standalone `compose` function required by Section 7.1 of the challenge brief.
"""

from __future__ import annotations
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from composer import compose as core_compose
from conversation_handlers import ConversationState, respond as core_respond


app = FastAPI(
    title="Vera Merchant AI Assistant",
    description="magicpin AI Challenge submission bot endpoint",
    version="1.0.0"
)

START_TIME = time.time()

# Thread-safe in-memory stores
# (scope, context_id) -> {"version": int, "payload": dict}
contexts: Dict[Tuple[str, str], Dict[str, Any]] = {}

# conversation_id -> ConversationState
conversations: Dict[str, ConversationState] = {}


# =============================================================================
# STANDALONE COMPOSE FUNCTION (Challenge Brief §7.1)
# =============================================================================

def compose(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None
) -> dict:
    """
    Public composition entrypoint.
    Returns: {body, cta, send_as, suppression_key, rationale, template_name, template_params}
    """
    return core_compose(category, merchant, trigger, customer)


# =============================================================================
# 1. HEALTHZ & METADATA ENDPOINTS
# =============================================================================

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _), _ in contexts.items():
        if scope in counts:
            counts[scope] += 1
        else:
            counts[scope] = 1

    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": counts
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Divya Srivastava Ai",
        "team_members": ["Divya Srivastava"],
        "model": "deterministic-hybrid-composer",
        "approach": "Deterministic 4-context composition with trigger routing, multi-turn state machine, and auto-reply suppression",
        "contact_email": "divyasrivastava2905@gmail.com",
        "version": "1.0.0",
        "submitted_at": "2026-04-26T08:00:00Z"
    }


# =============================================================================
# 2. CONTEXT INGESTION ENDPOINT
# =============================================================================

class ContextPayload(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: Dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: ContextPayload):
    valid_scopes = {"category", "merchant", "customer", "trigger"}
    if body.scope not in valid_scopes:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"accepted": False, "reason": "invalid_scope", "details": f"Scope must be one of {valid_scopes}"}
        )

    key = (body.scope, body.context_id)
    cur = contexts.get(key)

    # Idempotency and Versioning check
    if cur and cur["version"] >= body.version:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "accepted": False,
                "reason": "stale_version",
                "current_version": cur["version"]
            }
        )

    # Store atomically
    contexts[key] = {
        "version": body.version,
        "payload": body.payload
    }

    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": now_iso
    }


# =============================================================================
# 3. TICK ENDPOINT (OUTBOUND INITIATION)
# =============================================================================

class TickPayload(BaseModel):
    now: Optional[str] = None
    available_triggers: List[str] = Field(default_factory=list)


@app.post("/v1/tick")
async def tick(body: TickPayload):
    actions = []

    for trg_id in body.available_triggers:
        trg_entry = contexts.get(("trigger", trg_id))
        if not trg_entry:
            continue
        trg = trg_entry.get("payload", {})
        payload = trg.get("payload", {})

        # Resolve merchant
        merchant_id = trg.get("merchant_id") or payload.get("merchant_id")
        merchant_entry = contexts.get(("merchant", merchant_id)) if merchant_id else None
        merchant = merchant_entry.get("payload") if merchant_entry else None
        if not merchant:
            continue

        # Resolve customer if applicable
        customer_id = trg.get("customer_id") or payload.get("customer_id")
        customer = None
        if customer_id:
            cust_entry = contexts.get(("customer", customer_id))
            if cust_entry:
                customer = cust_entry.get("payload")

        # Resolve category
        cat_slug = merchant.get("category_slug") or payload.get("category", "")
        cat_entry = contexts.get(("category", cat_slug))
        category = cat_entry.get("payload") if cat_entry else {"slug": cat_slug, "voice": {}, "digest": [], "offer_catalog": []}

        # Compose message
        res = core_compose(category, merchant, trg, customer)

        conv_id = f"conv_{merchant_id}_{trg_id}"
        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": res["send_as"],
            "trigger_id": trg_id,
            "template_name": res.get("template_name", "vera_standard_v1"),
            "template_params": res.get("template_params", []),
            "body": res["body"],
            "cta": res["cta"],
            "suppression_key": res["suppression_key"],
            "rationale": res["rationale"]
        })

        # Register initial conversation state
        if conv_id not in conversations:
            conversations[conv_id] = ConversationState(
                conversation_id=conv_id,
                merchant_id=merchant_id,
                customer_id=customer_id
            )

    return {"actions": actions}


# =============================================================================
# 4. REPLY ENDPOINT (MULTI-TURN ENGAGEMENT)
# =============================================================================

class ReplyPayload(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str
    received_at: Optional[str] = None
    turn_number: int = 1


@app.post("/v1/reply")
async def reply(body: ReplyPayload):
    conv_id = body.conversation_id
    if conv_id not in conversations:
        conversations[conv_id] = ConversationState(
            conversation_id=conv_id,
            merchant_id=body.merchant_id,
            customer_id=body.customer_id
        )

    state = conversations[conv_id]
    response = core_respond(state, body.message)
    return response


# =============================================================================
# 5. TEARDOWN ENDPOINT (CLEANUP)
# =============================================================================

@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    return {"status": "ok", "message": "All state successfully wiped"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
