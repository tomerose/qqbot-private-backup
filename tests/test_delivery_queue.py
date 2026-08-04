import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from friend_core.delivery_queue import DeliveryEntry, DeliveryQueue  # noqa: E402


class _Reference:
    def __init__(self):
        self.updates = []

    def update(self, values):
        self.updates.append(values)


class _QueueDoc:
    id = "delivery-1"

    def __init__(self, path: str):
        self.reference = _Reference()
        self._data = {
            "local_path": path,
            "file_name": "edited.png",
            "kind": "image",
            "sender_id": "1211000567",
            "retry_count": 0,
            "next_retry_at": 0,
            "status": "pending",
        }

    def to_dict(self):
        return dict(self._data)


class DeliveryQueueTests(unittest.TestCase):
    def _run_poll(self, delivery_result: bool):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "edited.png"
            image.write_bytes(b"png")
            doc = _QueueDoc(str(image))
            queue = DeliveryQueue()
            queue._local_poll_entries = lambda _now: []
            queue._local_cleanup = lambda: None
            queue._db = object()
            queue._query_due_documents = lambda _now: [doc]
            queue._deliver_fn = AsyncMock(return_value=delivery_result)
            queue._send_fn = AsyncMock(return_value=True)
            processed = asyncio.run(queue.poll_and_retry())
            return processed, doc, queue

    def test_async_delivery_is_awaited_before_marking_success(self):
        processed, doc, queue = self._run_poll(True)

        self.assertEqual(processed, 1)
        self.assertEqual(doc.reference.updates[0]["status"], "delivered")
        queue._deliver_fn.assert_awaited_once()
        queue._send_fn.assert_awaited_once()

    def test_false_async_delivery_stays_pending_for_retry(self):
        processed, doc, queue = self._run_poll(False)

        self.assertEqual(processed, 1)
        self.assertNotIn("status", doc.reference.updates[0])
        self.assertEqual(doc.reference.updates[0]["retry_count"], 1)
        queue._deliver_fn.assert_awaited_once()
        queue._send_fn.assert_not_awaited()

    def test_retry_success_advances_real_task_ledger(self):
        class _Query:
            def where(self, *_args):
                return self

            def limit(self, _value):
                return self

            def stream(self):
                return []

        class _Document:
            def collection(self, _name):
                return _Query()

        class _Users:
            def document(self, _qq_id):
                return _Document()

        class _Database:
            def collection(self, _name):
                return _Users()

        queue = DeliveryQueue()
        queue._db = _Database()
        queue._task_tracker = Mock()
        entry = DeliveryEntry(
            sender_id="1211000567",
            job_id="task123",
            task_desc="编辑图片背景",
            task_owner="draw",
        )

        asyncio.run(queue._track_delivery_outcome(entry, "done", "qq:retry_queue"))

        queue._task_tracker.assert_called_once_with(
            "1211000567", "task123", "编辑图片背景", "done", "qq:retry_queue", "draw"
        )

    def test_owner_handler_receives_final_queue_outcome(self):
        class _Query:
            def document(self, _value):
                return self

            def collection(self, _value):
                return self

            def where(self, *_args):
                return self

            def limit(self, _value):
                return self

            def stream(self):
                return []

        queue = DeliveryQueue()
        queue._db = _Query()
        queue._task_tracker = Mock()
        handler = AsyncMock()
        queue.register_outcome_handler("agent", handler)
        entry = DeliveryEntry(
            sender_id="1211000567",
            job_id="agent123",
            task_desc="生成报告",
            task_owner="agent",
        )

        asyncio.run(queue._track_delivery_outcome(entry, "done", "qq:retry_queue"))

        handler.assert_awaited_once_with(entry, "done", "qq:retry_queue")
        queue._task_tracker.assert_called_once()

    def test_owner_handler_gets_partial_not_done_while_same_job_is_pending(self):
        class _Pending:
            def to_dict(self):
                return {"job_id": "agent123", "task_owner": "agent"}

        class _Query:
            def document(self, _value):
                return self

            def collection(self, _value):
                return self

            def where(self, *_args):
                return self

            def limit(self, _value):
                return self

            def stream(self):
                return [_Pending()]

        queue = DeliveryQueue()
        queue._db = _Query()
        queue._task_tracker = Mock()
        handler = AsyncMock()
        queue.register_outcome_handler("agent", handler)
        entry = DeliveryEntry(
            sender_id="1211000567",
            job_id="agent123",
            task_desc="生成两个报告",
            task_owner="agent",
        )

        asyncio.run(queue._track_delivery_outcome(entry, "done", "qq:retry_queue"))

        handler.assert_awaited_once_with(
            entry, "artifact_delivered", "qq:retry_queue"
        )
        queue._task_tracker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
