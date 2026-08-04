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
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    replacements = {
        "astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
        "astrbot.api.event": event, "astrbot.api.provider": provider,
        "astrbot.api.star": star,
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
    def test_group_and_private_chat_use_gemini(self):
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

        asyncio.run(router.route_provider(Event("group-a", "pro")))
        router._routes.clear()
        asyncio.run(router.route_provider(Event("group-b", "x")))
        router._routes.clear()
        asyncio.run(router.route_provider(Event("group-c", "ordinary")))
        router._routes.clear()
        asyncio.run(router.route_provider(Event("", "ordinary")))
        self.assertEqual(
            calls,
            [
                "gemini-3.6-flash",
                "gemini-3.6-flash",
                "gemini-3.6-flash",
                "gemini-3.6-flash",
            ],
        )

    def test_concurrent_first_messages_write_one_session_route(self):
        module = load_router()
        calls = []

        async def set_provider(provider, *_):
            calls.append(provider)
            await asyncio.sleep(0)

        context = types.SimpleNamespace(provider_manager=types.SimpleNamespace(set_provider=set_provider))
        router = module.ChatRouter(context)

        class Event:
            unified_msg_origin = "same-session"

            def get_group_id(self):
                return "group-a"

        async def run():
            await asyncio.gather(router.route_provider(Event()), router.route_provider(Event()))

        asyncio.run(run())
        self.assertEqual(calls, ["gemini-3.6-flash"])

    def test_short_same_speaker_followup_is_coalesced(self):
        module = load_router()
        module._REPLY_COALESCE_DELAY_SECONDS = 0.01
        router = module.ChatRouter(types.SimpleNamespace())

        class Event:
            def __init__(self, text):
                self.unified_msg_origin = "same-session"
                self.message_str = text
                self.message_obj = types.SimpleNamespace(message_str=text)
                # AstrBot sets this for ordinary private messages too.  They
                # must remain eligible for the brief follow-up window.
                self.is_at_or_wake_command = True
                self.stopped = False

            def get_sender_id(self):
                return "1211000567"

            def is_private_chat(self):
                return True

            def get_message_str(self):
                return self.message_str

            def get_messages(self):
                return []

            def stop_event(self):
                self.stopped = True

            def set_extra(self, *_args):
                pass

        async def run():
            first = Event("我刚想说")
            second = Event("还有件事")
            first_task = asyncio.create_task(router.coalesce_followup_messages(first))
            await asyncio.sleep(0)
            await router.coalesce_followup_messages(second)
            await first_task
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first.message_str, "我刚想说\n还有件事")
        self.assertEqual(first.message_obj.message_str, first.message_str)
        self.assertTrue(second.stopped)

    def test_command_and_urgent_message_skip_coalescing(self):
        module = load_router()
        router = module.ChatRouter(types.SimpleNamespace())

        class Event:
            is_at_or_wake_command = False

            def __init__(self, text):
                self.text = text

            def get_message_str(self):
                return self.text

        self.assertEqual(router._reply_coalesce_text(Event("/help")), "")
        self.assertEqual(router._reply_coalesce_text(Event("我不想活了")), "")

    def test_group_wake_message_skips_coalescing(self):
        module = load_router()
        router = module.ChatRouter(types.SimpleNamespace())

        class Event:
            is_at_or_wake_command = True

            def get_message_str(self):
                return "小柠在吗"

            def is_private_chat(self):
                return False

        self.assertEqual(router._reply_coalesce_text(Event()), "")

    def test_private_problem_solving_uses_the_single_chat_model(self):
        module = load_router()
        calls = []

        async def set_provider(provider, *_args):
            calls.append(provider)

        router = module.ChatRouter(
            types.SimpleNamespace(
                provider_manager=types.SimpleNamespace(set_provider=set_provider)
            )
        )

        class Event:
            unified_msg_origin = "private-session"

            @staticmethod
            def get_group_id():
                return ""

            @staticmethod
            def get_message_str():
                return "帮我解这道数学题"

        asyncio.run(router.route_provider(Event()))
        self.assertEqual(calls, ["gemini-3.6-flash"])


if __name__ == "__main__":
    unittest.main()
