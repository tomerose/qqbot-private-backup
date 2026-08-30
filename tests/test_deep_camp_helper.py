import asyncio
import sys
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from deep_camp_helper import main as deep_main  # noqa: E402
from deep_camp_helper.main import DEEP_GROUP, DeepCampHelper  # noqa: E402


class Event:
    def __init__(self, text, group_id=DEEP_GROUP):
        self.text = text
        self.group_id = group_id

    def get_group_id(self):
        return self.group_id

    def get_message_str(self):
        return self.text

    def chain_result(self, chain):
        return chain

    def stop_event(self):
        self.stopped = True


class DeepCampHelperTests(unittest.TestCase):
    def test_legacy_command_uses_current_event_shape(self):
        async def scenario():
            plugin = DeepCampHelper.__new__(DeepCampHelper)
            original = deep_main.HAS_API
            deep_main.HAS_API = False
            try:
                event = Event("/tasks")
                event.stopped = False
                replies = [item async for item in plugin.on_message(event)]
                self.assertEqual(len(replies), 1)
                self.assertTrue(event.stopped)
            finally:
                deep_main.HAS_API = original

        asyncio.run(scenario())

    def test_unrelated_slash_command_is_not_consumed(self):
        async def scenario():
            plugin = DeepCampHelper.__new__(DeepCampHelper)
            event = Event("/hello")
            event.stopped = False
            replies = [item async for item in plugin.on_message(event)]
            self.assertEqual(replies, [])
            self.assertFalse(event.stopped)

        asyncio.run(scenario())

    def test_submit_link_works_without_realtime_api(self):
        async def scenario():
            plugin = DeepCampHelper.__new__(DeepCampHelper)
            event = Event("/submit")
            event.stopped = False
            original = deep_main.HAS_API
            deep_main.HAS_API = False
            try:
                replies = [item async for item in plugin.on_message(event)]
            finally:
                deep_main.HAS_API = original
            self.assertTrue(event.stopped)
            self.assertEqual(len(replies), 1)
            self.assertIn("github.com/tomerose/deep-camp-phase1-liu", replies[0][0].text)

        asyncio.run(scenario())

    def test_deep_context_is_injected_only_for_its_group(self):
        class Request:
            system_prompt = ""

        async def scenario():
            plugin = DeepCampHelper.__new__(DeepCampHelper)
            request = Request()
            await plugin.inject_deep_context(Event("任务怎么做"), request)
            self.assertIn("\u3010DEEP \u8425\u5730\u77e5\u8bc6\u3011", request.system_prompt)
            other_request = Request()
            await plugin.inject_deep_context(Event("任务怎么做", "1"), other_request)
            self.assertEqual(other_request.system_prompt, "")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
