import asyncio
import sys
import unittest
from pathlib import Path


PLUGIN_DIR = (
    Path(__file__).resolve().parents[1]
    / "astrbot"
    / "data"
    / "plugins"
    / "claude_code_agent"
)
sys.path.insert(0, str(PLUGIN_DIR))

from backend_health import BackendHealthCache, backend_probe_command  # noqa: E402


class BackendHealthTests(unittest.TestCase):
    def test_probe_commands_are_non_mutating_version_checks(self):
        for backend in ("claude", "codex", "workbuddy"):
            command = backend_probe_command(backend)
            self.assertIn("--version", command)
            self.assertNotIn("exec", command)
            self.assertNotIn("-p", command)

    def test_only_successful_backends_are_available_and_cache_is_reused(self):
        calls = []

        async def runner(command, timeout):
            calls.append(tuple(command))
            return 0 if "claude" in " ".join(command).lower() else 1

        async def scenario():
            cache = BackendHealthCache(ttl_seconds=60, runner=runner)
            first = await cache.available()
            second = await cache.available()
            return first, second

        first, second = asyncio.run(scenario())

        self.assertEqual(first, frozenset({"claude"}))
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 3)

    def test_probe_exception_fails_closed(self):
        async def runner(command, timeout):
            raise OSError("missing")

        available = asyncio.run(
            BackendHealthCache(ttl_seconds=60, runner=runner).available()
        )

        self.assertEqual(available, frozenset())


if __name__ == "__main__":
    unittest.main()
