"""Voice-message detection kept independent for unit testing."""

from __future__ import annotations


def contains_voice_component(components: object) -> bool:
    """Return True when a message chain contains an AstrBot Record segment."""
    if not components:
        return False
    for component in components if isinstance(components, (list, tuple)) else []:
        if type(component).__name__.lower() == "record":
            return True
        component_type = getattr(component, "type", None)
        if component_type is not None and str(component_type).rsplit(".", 1)[-1].lower() == "record":
            return True
        if isinstance(component, dict) and str(component.get("type", "")).lower() == "record":
            return True
    return False
