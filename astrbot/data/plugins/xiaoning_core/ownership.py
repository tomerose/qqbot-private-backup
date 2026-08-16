"""Compatibility gate for legacy natural-language handlers during migration."""

from __future__ import annotations


def route_allows(event, owner: str) -> bool:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return True
    if not bool(getter("xiaoning_enforce_ownership", False)):
        return True
    selected = str(getter("xiaoning_route_owner", "") or "")
    return not selected or selected == str(owner)
