import sys
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from friend_core import commitment_tracker  # noqa: E402
from friend_core.commitment_tracker import _extract_sentences  # noqa: E402


class _Event:
    def __init__(self, text):
        self._text = text

    def get_sender_id(self):
        return "1211000567"

    def get_message_str(self):
        return self._text


class _Request:
    system_prompt = "基础提示"


class _Tracker:
    def build_injection(self, _sender):
        return "\n\n【未兑现的约定】\n- 明天我再帮你看方案"


class CommitmentTrackerTests(unittest.TestCase):
    def test_execution_progress_words_are_not_durable_promises(self):
        for reply in (
            "我这就调用去水印功能，稍等一下。",
            "我马上帮你发修好的图片。",
            "等我一下，我正在处理。",
        ):
            self.assertEqual(_extract_sentences(reply), [])

    def test_real_future_commitments_are_still_detected(self):
        self.assertEqual(len(_extract_sentences("明天我再帮你看一下这个方案。")), 1)

    def test_pending_commitments_only_inject_on_followup(self):
        quiet = _Request()
        followup = _Request()
        with patch.object(commitment_tracker, "get_tracker", return_value=_Tracker()):
            asyncio.run(commitment_tracker.on_llm_request_inject(_Event("今天心情不错"), quiet))
            asyncio.run(commitment_tracker.on_llm_request_inject(_Event("你上次说要帮我看方案"), followup))

        self.assertNotIn("未兑现的约定", quiet.system_prompt)
        self.assertIn("未兑现的约定", followup.system_prompt)


if __name__ == "__main__":
    unittest.main()
