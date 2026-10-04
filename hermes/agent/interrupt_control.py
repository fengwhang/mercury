"""Interrupt attribution helpers for ``AIAgent`` (parity port of stock ``agent/interrupt_control.py``).

Minimal closure for the ported ``turn_*`` modules: the interrupt-issuer attribution and the
turn-exit reason for an API call cut short by an interrupt. The remaining stock control surface
(steer/redirect queues, fence helpers) stays in Mercury's consolidated ``run_agent.AIAgent``.
"""
from typing import Optional

# ``interrupt()`` categories that mean a human stopped the turn. Any other ``_tool_interrupt_reason`` was
# supplied by a system producer via ``tool_reason`` (watchdogs, lease loss, lifecycle cancellation) and is
# attributed to it in the turn exit reason instead of being booked as a user stop (#112647).
_REASON_HARD_STOP = "explicit stop requested"
_REASON_NEW_MESSAGE = "user sent a new message"
_REASON_USER_INTERRUPT = "user interrupt"
USER_INTERRUPT_REASONS = frozenset({_REASON_HARD_STOP, _REASON_NEW_MESSAGE, _REASON_USER_INTERRUPT})


def interrupt_issuer(agent) -> Optional[str]:
    """Slug of the system producer behind the pending interrupt, or ``None`` for a human stop."""
    reason = getattr(agent, "_tool_interrupt_reason", None)
    if not reason or reason in USER_INTERRUPT_REASONS:
        return None
    return str(reason).strip().replace(" ", "_")


def interrupted_during_api_call_reason(agent) -> str:
    """Turn exit reason for an API call cut short by an interrupt (``turn_explainers`` matches the prefix)."""
    issuer = interrupt_issuer(agent)
    return f"interrupted_during_api_call({issuer})" if issuer else "interrupted_during_api_call"
