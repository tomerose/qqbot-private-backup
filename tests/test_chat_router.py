import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_router():
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = types.SimpleNamespace(
        platform_adapter_type=lambda *args, **kwargs: lambda fn: fn,
        PlatformAdapterType=types.SimpleNamespace(ALL="all"),
    )
    provider = types.ModuleType("astrbot.api.provider")
    provider.ProviderType = types.SimpleNamespace(CHAT_COMPLETION="chat")
    star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star.Context = object
    star.Star = Star
    access = types.ModuleType("draw_command.pro_access")
    access.Tier = types.SimpleNamespace(PRO="pro", X="x")
    access.get_tier = lambda *_: "ordinary"
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    replacements = {
        "astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
        "astrbot.api.event": event, "astrbot.api.provider": provider,
        "astrbot.api.star": star, "draw_command": types.ModuleType("draw_command"),
        "draw_command.pro_access": access,
    }
    previous = {name: sys.modules.get(name) for name in replacements}
    try:
        sys.modules.update(replacements)
        spec = importlib.util.spec_from_file_location("chat_router_test", ROOT / "astrbot/data/plugins/chat_router/main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
    return module


class ChatRouterTests(unittest.TestCase):
    def test_only_private_pro_uses_gemini(self):
        module = load_router()
        calls = []
        async def set_provider(provider, *_):
            calls.append(provider)

        context = types.SimpleNamespace(provider_manager=types.SimpleNamespace(set_provider=set_provider))
        router = module.ChatRouter(context)

        class Event:
            unified_msg_origin = "session"
            def __init__(self, group, sender): self.group, self.sender = group, sender
            def get_group_id(self): return self.group
            def get_sender_id(self): return self.sender

        module.get_tier = lambda sender, _: {
            "pro": module.Tier.PRO, "x": module.Tier.X,
        }.get(sender, "ordinary")
        asyncio.run(router.route_provider(Event("group", "pro")))
        asyncio.run(router.route_provider(Event("", "ordinary")))
        asyncio.run(router.route_provider(Event("", "go")))
        asyncio.run(router.route_provider(Event("", "pro")))
        self.assertEqual(calls, ["deepseek-chat", "gemini-2.5-flash"])


if __name__ == "__main__":
    unittest.main()
