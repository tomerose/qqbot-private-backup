import asyncio
import sys
import unittest
from pathlib import Path


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from xiaoning_runtime import defer_stop_event  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
