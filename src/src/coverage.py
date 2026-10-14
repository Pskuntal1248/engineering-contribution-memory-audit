"""Optional source coverage is distinct from failed contribution reads."""
from __future__ import annotations


def unavailable(text: str) -> bool:
    low = str(text).lower()
    return any(x in low for x in ("not connected", "no connection", "needs_reconnect", "unauthorized", "unauthorised", "401", "not available in this run", "not allowed in this run"))


def line(status: dict[str, str]) -> str:
    effects = {"jira": "ticket patterns and ticket-only work weren't checked", "slack": "debugging, help and discussions weren't checked", "docs": "written designs and doc contributions weren't checked"}
    parts = []
    for source in ("jira", "slack", "docs"):
        reason = status.get(source)
        if reason and reason != "covered":
            parts.append(f"{source.capitalize()} {reason}; {effects[source]}")
    return "Not covered: " + ". ".join(parts) + ". Connect or select these sources to add them." if parts else ""
