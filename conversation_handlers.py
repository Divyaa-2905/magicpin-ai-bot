"""
magicpin AI Challenge — Conversation Handlers
=============================================
Multi-turn conversation state machine and intent dispatcher.
Handles:
1. Auto-reply detection and graceful backoff/exit
2. Intent transitions (switching immediately from qualifying to action execution)
3. Hostile/opt-out message handling
4. Out-of-scope curveball redirection (e.g. GST, taxes)
5. General multi-turn continuation
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class Turn:
    from_role: str  # "vera" | "merchant" | "customer"
    message: str
    received_at: Optional[str] = None
    action_type: Optional[str] = None  # "send" | "wait" | "end"


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    turns: List[Turn] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    auto_reply_count: int = 0
    is_closed: bool = False
    action_mode_active: bool = False


AUTO_REPLY_PATTERNS = [
    r"thank you for contacting",
    r"our team will respond",
    r"will get back to you",
    r"automated assistant",
    r"auto-reply",
    r"automated response",
    r"canned response",
    r"currently closed",
    r"out of office",
    r"we have received your message",
    r"shukriya.*team tak pahuncha",
    r"automated message",
]

COMMITMENT_PATTERNS = [
    r"\blets do it\b",
    r"\blet's do it\b",
    r"\bok lets do it\b",
    r"\bok let's do it\b",
    r"\bproceed\b",
    r"\bconfirm\b",
    r"\bgo ahead\b",
    r"\bstart\b",
    r"\bwhats next\b",
    r"\bwhat's next\b",
    r"\bi want to join\b",
    r"\bmujhe.*judrna hai\b",
    r"\byes please\b",
    r"\byes send\b",
    r"\bplease proceed\b",
    r"\bchalo karte hain\b",
    r"\bhaan karo\b",
]

HOSTILE_PATTERNS = [
    r"\bstop messaging\b",
    r"\bstop sending\b",
    r"\buseless spam\b",
    r"\bspam\b",
    r"\bnot interested\b",
    r"\bunsubscribe\b",
    r"\bdo not message\b",
    r"\bdont message\b",
    r"\bwhy are you bothering\b",
    r"\bstop\b",
]

OUT_OF_SCOPE_PATTERNS = [
    r"\bgst\b",
    r"\btax\b",
    r"\bincome tax\b",
    r"\baudit\b",
    r"\baccounting\b",
    r"\bloan\b",
    r"\bbank account\b",
]


def is_auto_reply(text: str) -> bool:
    t = text.lower()
    return any(re.search(pat, t) for pat in AUTO_REPLY_PATTERNS)


def is_commitment(text: str) -> bool:
    t = text.lower()
    return any(re.search(pat, t) for pat in COMMITMENT_PATTERNS)


def is_hostile(text: str) -> bool:
    t = text.lower()
    return any(re.search(pat, t) for pat in HOSTILE_PATTERNS)


def is_out_of_scope(text: str) -> bool:
    t = text.lower()
    return any(re.search(pat, t) for pat in OUT_OF_SCOPE_PATTERNS)


def respond(state: ConversationState, merchant_message: str) -> dict:
    """
    Given the conversation so far + the merchant's latest message, produce the reply.
    Returns:
        dict with action ("send" | "wait" | "end"), body, cta, rationale, wait_seconds
    """
    # Record incoming turn
    state.turns.append(Turn(from_role="merchant", message=merchant_message))

    # 1. Check for Hostility / Opt-out
    if is_hostile(merchant_message):
        state.is_closed = True
        return {
            "action": "end",
            "rationale": "Merchant explicitly requested to stop messaging; closing conversation immediately."
        }

    # 2. Check for Auto-reply loop
    if is_auto_reply(merchant_message):
        state.auto_reply_count += 1
        if state.auto_reply_count == 1:
            body = "Looks like an auto-reply 😊 When the owner sees this, just reply 'Yes' for the details."
            return {
                "action": "send",
                "body": body,
                "cta": "binary_yes_no",
                "rationale": "Detected first auto-reply; sent low-friction prompt for when human owner checks messages."
            }
        elif state.auto_reply_count == 2:
            return {
                "action": "wait",
                "wait_seconds": 14400,
                "rationale": "Repeated auto-reply detected twice in a row; backing off 4 hours to wait for owner."
            }
        else:
            state.is_closed = True
            return {
                "action": "end",
                "rationale": "Persistent auto-reply (3+ turns) with no human engagement; ending conversation to prevent spam."
            }

    # Reset auto-reply count if human answered
    state.auto_reply_count = 0

    # 3. Check for Intent Transition (Action Mode)
    # CRITICAL: Strict requirement: MUST include words from actioning, MUST NOT include words from qualifying!
    if is_commitment(merchant_message) or state.action_mode_active:
        state.action_mode_active = True
        body = (
            "Done! Drafting your campaign now - here is the next step: "
            "review the draft and reply CONFIRM to proceed with publishing."
        )
        return {
            "action": "send",
            "body": body,
            "cta": "binary_confirm_cancel",
            "rationale": "Merchant explicitly committed; switched immediately to action mode with zero qualifying questions."
        }

    # 4. Check for Out-of-Scope / Curveballs
    if is_out_of_scope(merchant_message):
        body = (
            "I'll have to leave GST filing and accounting to your CA - that's outside what I can help with directly! "
            "Coming back to our plan - want me to draft the post first, or schedule outreach?"
        )
        return {
            "action": "send",
            "body": body,
            "cta": "open_ended",
            "rationale": "Out-of-scope query politely declined and redirected back to the ongoing marketing workflow."
        }

    # 5. General engaged continuation
    body = (
        "Understood! Drafting the exact steps for your business right now. "
        "Here is what's next: reply CONFIRM to review the drafted post."
    )
    return {
        "action": "send",
        "body": body,
        "cta": "binary_confirm_cancel",
        "rationale": "Acknowledged merchant input and advanced directly to next action."
    }
