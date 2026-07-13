import asyncio
import sys
import unittest
from pathlib import Path


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from xiaoning_runtime import chat_response_content, defer_stop_event  # noqa: E402


class Event:
    def __init__(self):
        self.stopped = False

    def stop_event(self):
        self.stopped = True


class Handler:
    @defer_stop_event
    async def handle(self, event):
        event.stop_event()
        yield "progress"
        yield "final"


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class DeferStopEventTests(unittest.TestCase):
    def test_stops_only_after_all_results_are_yielded(self):
        async def scenario():
            event = Event()
            stream = Handler().handle(event)

            self.assertEqual(await anext(stream), "progress")
            self.assertFalse(event.stopped)
            self.assertEqual(await anext(stream), "final")
            self.assertFalse(event.stopped)
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
            self.assertTrue(event.stopped)

        asyncio.run(scenario())

    def test_chat_response_uses_error_envelope_instead_of_key_error(self):
        with self.assertRaisesRegex(RuntimeError, "上游服务繁忙"):
            chat_response_content(Response(502, {"error": {"message": "上游服务繁忙"}}))

    def test_chat_response_returns_completion_content(self):
        response = Response(200, {"choices": [{"message": {"content": "  正常结果  "}}]})
        self.assertEqual(chat_response_content(response), "正常结果")


if __name__ == "__main__":
    unittest.main()
