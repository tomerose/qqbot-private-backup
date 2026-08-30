from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from xiaoning_core.outbound_history import record_confirmed_outbound  # noqa: E402


class OutboundHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "data_v4.db"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE conversations (
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    inner_conversation_id INTEGER PRIMARY KEY,
                    conversation_id TEXT NOT NULL UNIQUE,
                    platform_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    content TEXT,
                    title TEXT,
                    persona_id TEXT,
                    token_usage INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE preferences (
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    id INTEGER PRIMARY KEY,
                    scope TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    "key" TEXT NOT NULL,
                    value TEXT NOT NULL,
                    UNIQUE(scope, scope_id, "key")
                );
                """
            )
            for index, user in enumerate(("10001", "10002"), 1):
                umo = f"llbot-1:FriendMessage:{user}"
                cid = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO conversations(
                           inner_conversation_id, conversation_id, platform_id,
                           user_id, content, token_usage
                       ) VALUES (?, ?, 'llbot-1', ?, '[]', 0)""",
                    (index, cid, umo),
                )
                connection.execute(
                    """INSERT INTO preferences(id, scope, scope_id, "key", value)
                       VALUES (?, 'umo', ?, 'sel_conv_id', ?)""",
                    (index, umo, json.dumps({"val": cid})),
                )

    def tearDown(self):
        self.temp.cleanup()

    def _history(self, user: str) -> list[dict]:
        umo = f"llbot-1:FriendMessage:{user}"
        with closing(sqlite3.connect(self.db)) as connection:
            raw = connection.execute(
                "SELECT content FROM conversations WHERE user_id=?", (umo,)
            ).fetchone()[0]
        return json.loads(raw)

    def test_confirmed_delivery_is_scoped_and_idempotent(self):
        umo = "llbot-1:FriendMessage:10001"
        first = record_confirmed_outbound(
            self.db,
            umo=umo,
            text="今天的小柠宣言",
            delivery_id="qq-message-77",
            confirmed=True,
        )
        duplicate = record_confirmed_outbound(
            self.db,
            umo=umo,
            text="今天的小柠宣言",
            delivery_id="qq-message-77",
            confirmed=True,
        )
        adopted = record_confirmed_outbound(
            self.db,
            umo=umo,
            text="今天的小柠宣言",
            delivery_id="qq-message-78",
            confirmed=True,
        )

        self.assertEqual(first.status, "recorded")
        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(adopted.status, "duplicate_content")
        self.assertEqual(len(self._history("10001")), 2)
        self.assertEqual(self._history("10002"), [])

    def test_unconfirmed_delivery_never_enters_memory(self):
        result = record_confirmed_outbound(
            self.db,
            umo="llbot-1:FriendMessage:10001",
            text="这条并没有真正送达",
            delivery_id="failed-1",
            confirmed=False,
        )
        self.assertEqual(result.status, "unconfirmed")
        self.assertEqual(self._history("10001"), [])

    def test_selected_conversation_must_belong_to_exact_session(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            other_cid = connection.execute(
                "SELECT conversation_id FROM conversations WHERE user_id=?",
                ("llbot-1:FriendMessage:10002",),
            ).fetchone()[0]
            connection.execute(
                """UPDATE preferences SET value=?
                   WHERE scope_id=? AND "key"='sel_conv_id'""",
                (
                    json.dumps({"val": other_cid}),
                    "llbot-1:FriendMessage:10001",
                ),
            )

        with self.assertRaises(ValueError):
            record_confirmed_outbound(
                self.db,
                umo="llbot-1:FriendMessage:10001",
                text="不能串到别人会话",
                delivery_id="wrong-cid",
                confirmed=True,
            )
        self.assertEqual(self._history("10001"), [])
        self.assertEqual(self._history("10002"), [])
