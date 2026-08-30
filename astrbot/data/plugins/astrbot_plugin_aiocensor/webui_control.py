"""Small, dependency-free WebUI startup policy."""


def webui_enabled(config: object) -> bool:
    if not hasattr(config, "get"):
        return False
    webui = config.get("webui", {})
    return bool(webui.get("enable", False)) if hasattr(webui, "get") else False
