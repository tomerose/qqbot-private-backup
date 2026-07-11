import sys
import tempfile
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from claude_code_agent.access_policy import AccessPolicy, AccessTier  # noqa: E402
from claude_code_agent.experience_memory import (  # noqa: E402
    ExperienceMemoryStore,
    MemoryEntry,
    MemoryKind,
    validate_memory_entry,
)


class ExperienceMemoryTests(unittest.TestCase):
    def test_sensitive_memory_is_rejected(self):
        for value in (
            r"目录 D:\private",
            "token=abcdef1234567890",
            "保存我们的私聊全文",
            "记住我的病历",
        ):
            with self.subTest(value=value):
                decision = validate_memory_entry(
                    MemoryEntry(MemoryKind.PREFERENCE, "tone", value),
                    explicit_request=True,
                    is_pro=True,
                )
                self.assertFalse(decision.allowed)

    def test_memory_cannot_grant_pro(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceMemoryStore(Path(tmp))
            store.put(
                owner="99999",
                entry=MemoryEntry(MemoryKind.PREFERENCE, "reply_length", "short"),
                explicit_request=True,
                is_pro=False,
            )

            self.assertIs(
                AccessPolicy(("1211000567",)).resolve_tier("99999"),
                AccessTier.ORDINARY,
            )

    def test_relationship_fact_requires_explicit_pro_request(self):
        entry = MemoryEntry(
            MemoryKind.RELATIONSHIP,
            "relationship_fact",
            "小姚是小江的宝",
        )

        self.assertFalse(
            validate_memory_entry(
                entry, explicit_request=False, is_pro=True
            ).allowed
        )
        self.assertTrue(
            validate_memory_entry(
                entry, explicit_request=True, is_pro=True
            ).allowed
        )

    def test_store_encrypts_value_and_can_read_it_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ExperienceMemoryStore(root)
            entry = MemoryEntry(MemoryKind.PREFERENCE, "tone", "简短自然")

            store.put(
                owner="1211000567",
                entry=entry,
                explicit_request=True,
                is_pro=True,
            )

            files = list(root.glob("*.bin"))
            self.assertEqual(len(files), 1)
            self.assertNotIn(entry.value.encode("utf-8"), files[0].read_bytes())
            self.assertEqual(store.get("1211000567", "tone"), entry)


if __name__ == "__main__":
    unittest.main()
