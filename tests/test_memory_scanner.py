import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"))

from friend_core.memory_scanner import MemoryScanner  # noqa: E402


class MemoryScannerTests(unittest.TestCase):
    def test_firestore_snapshot_is_read_off_event_loop(self):
        async def scenario():
            scanner = MemoryScanner()
            scanner._read_all_memories = Mock(return_value=[("1", [{"key": "exam"}])])
            scanner._extract_events = AsyncMock(return_value=[{"qq_id": "1"}])
            with patch("friend_core.memory_scanner.asyncio.to_thread", new_callable=AsyncMock) as to_thread:
                to_thread.side_effect = lambda func, *args, **kwargs: func(*args, **kwargs)
                tasks = await scanner.scan_all_users()
            self.assertEqual(tasks, [{"qq_id": "1"}])
            to_thread.assert_awaited_once_with(scanner._read_all_memories)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
