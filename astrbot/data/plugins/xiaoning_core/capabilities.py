"""Typed facade over the existing canonical capability catalog."""

from __future__ import annotations

from collections.abc import Iterable

from .models import CapabilitySpec, RiskLevel, RouteKind

try:
    from xiaoning_capabilities import CAPABILITIES
except ImportError:
    from data.plugins.xiaoning_capabilities import CAPABILITIES


_TASK_CAPABILITIES = {"document"}
_CHAT_CAPABILITIES = {"chat"}
_MEDIUM_RISK = {"document", "web", "video_production", "video_workshop"}


class CapabilityRegistry:
    def __init__(self, specs: Iterable[CapabilitySpec] | None = None):
        source = tuple(specs) if specs is not None else tuple(self._from_catalog())
        self._by_id = {item.capability_id: item for item in source}
        if len(self._by_id) != len(source):
            raise ValueError("duplicate capability id")

    @staticmethod
    def _from_catalog() -> Iterable[CapabilitySpec]:
        for item in CAPABILITIES:
            route_kind = RouteKind.CAPABILITY
            if item.id in _TASK_CAPABILITIES:
                route_kind = RouteKind.TASK
            elif item.id in _CHAT_CAPABILITIES:
                route_kind = RouteKind.CHAT
            yield CapabilitySpec(
                capability_id=item.id,
                owner=item.owner,
                route_kind=route_kind,
                risk=RiskLevel.MEDIUM if item.id in _MEDIUM_RISK else RiskLevel.LOW,
                eligible_tiers=item.tiers,
                keywords=item.keywords,
                command=item.guide_token,
                artifact_types=item.artifacts,
                delivery_required=bool(item.artifacts),
                handler=f"{item.owner}:handle",
            )

    def get(self, capability_id: str) -> CapabilitySpec | None:
        return self._by_id.get(str(capability_id))

    def all(self) -> tuple[CapabilitySpec, ...]:
        return tuple(self._by_id.values())

    def matches(self, text: str) -> list[tuple[int, CapabilitySpec]]:
        value = str(text or "").casefold()
        matches: list[tuple[int, CapabilitySpec]] = []
        for item in self._by_id.values():
            lengths = [len(word) for word in item.keywords if word.casefold() in value]
            command = item.command.strip()
            if command and value.startswith(command.casefold()):
                lengths.append(1000 + len(command))
            if lengths:
                matches.append((max(lengths), item))
        return sorted(matches, key=lambda pair: (-pair[0], pair[1].capability_id))
