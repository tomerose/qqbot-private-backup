import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(r"D:\Claudecoda学习\qqbot\astrbot\data\plugins\claude_code_agent")
sys.path.insert(0, str(PLUGIN_DIR))

from job_store import JobStore  # noqa: E402


class JobStoreTests(unittest.TestCase):
    def test_job_ledger_persists_metadata_without_prompt_or_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "jobs.db"
            store = JobStore(db_path)
            store.start(
                job_id="a1b2c3d4e5f6",
                owner_id="1211000567",
                scope="group:945598390",
                task=r"读取 C:\private\secret.txt，密码是 never-store-this",
                backend="claude",
                risk="读取凭据",
                now=1000,
            )
            record = store.get("a1b2c3d4e5f6")
            self.assertEqual(record["state"], "running")
            self.assertEqual(len(record["task_digest"]), 64)

            raw = db_path.read_bytes()
            self.assertNotIn(b"never-store-this", raw)
            self.assertNotIn(b"private", raw)

            conn = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            finally:
                conn.close()
            self.assertNotIn("task", columns)
            self.assertNotIn("work_dir", columns)
            self.assertNotIn("result", columns)

    def test_restart_marks_running_jobs_interrupted_and_terminal_jobs_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.db")
            store.start("a1b2c3d4e5f6", "owner", "private", "task one", "claude", "", now=1000)
            store.start("b1b2c3d4e5f6", "owner", "private", "task two", "codex", "", now=1000)
            store.finish("b1b2c3d4e5f6", "completed", exit_code=0, deliverable_count=2, now=1010)

            self.assertEqual(store.recover_interrupted(now=1020), 1)
            self.assertEqual(store.get("a1b2c3d4e5f6")["state"], "interrupted")
            self.assertEqual(store.get("b1b2c3d4e5f6")["state"], "completed")
            self.assertEqual(store.get("b1b2c3d4e5f6")["stage"], "completed")
            self.assertEqual(store.get("b1b2c3d4e5f6")["deliverable_count"], 2)

    def test_state_machine_allows_only_declared_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.db")
            store.start(
                "a1b2c3d4e5f6",
                "owner",
                "private",
                "read project",
                "claude",
                "",
                state="queued",
                recovery="replay_safe",
                now=1000,
            )
            with self.assertRaisesRegex(ValueError, "迁移"):
                store.transition("a1b2c3d4e5f6", "completed", "completed", now=1001)

            store.transition("a1b2c3d4e5f6", "running", "executing", now=1002)
            store.transition("a1b2c3d4e5f6", "verifying", "verifying", now=1003)
            store.transition("a1b2c3d4e5f6", "delivering", "delivering", now=1004)
            store.transition("a1b2c3d4e5f6", "completed", "completed", now=1005)
            record = store.get("a1b2c3d4e5f6")
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["stage"], "completed")
            self.assertEqual(record["recovery"], "replay_safe")

    def test_interrupted_records_can_be_listed_without_prompt_or_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.db")
            store.start(
                "a1b2c3d4e5f6",
                "owner",
                "group:secret",
                "private task text",
                "claude",
                "",
                state="running",
                recovery="replay_safe",
                now=1000,
            )
            store.recover_interrupted(now=1010)
            records = store.list_interrupted()
            self.assertEqual([item["job_id"] for item in records], ["a1b2c3d4e5f6"])
            self.assertNotIn("task", records[0])
            self.assertNotIn("scope", records[0])
            self.assertEqual(records[0]["recovery"], "replay_safe")

    def test_delivery_digest_is_validated_and_contains_no_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.db")
            store.start("a1b2c3d4e5f6", "owner", "private", "task", "claude", "", now=1000)
            digest = "a" * 64
            store.record_delivery("a1b2c3d4e5f6", digest, now=1010)
            self.assertEqual(store.get("a1b2c3d4e5f6")["delivery_digest"], digest)
            with self.assertRaisesRegex(ValueError, "摘要"):
                store.record_delivery("a1b2c3d4e5f6", r"D:\private\answer.txt")

    def test_legacy_database_is_migrated_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "jobs.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY, owner_fingerprint TEXT NOT NULL,
                        scope_fingerprint TEXT NOT NULL, task_digest TEXT NOT NULL,
                        backend TEXT NOT NULL, state TEXT NOT NULL, risk TEXT NOT NULL DEFAULT '',
                        exit_code INTEGER, deliverable_count INTEGER NOT NULL DEFAULT 0,
                        error_code TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )"""
                )
                conn.execute(
                    "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("legacy", "owner", "scope", "digest", "claude", "completed", "", 0, 0, "", 1.0, 2.0),
                )
                conn.commit()
            finally:
                conn.close()

            store = JobStore(db_path)
            record = store.get("legacy")
            self.assertEqual(record["state"], "completed")
            self.assertIn("stage", record)
            self.assertIn("recovery", record)
            self.assertIn("delivery_digest", record)

    def test_planned_job_tracks_bounded_step_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.db")
            store.start(
                "a1b2c3d4e5f6",
                "owner",
                "private",
                "read then summarize",
                "claude",
                "",
                state="planned",
                step_count=2,
                now=1000,
            )
            store.transition(
                "a1b2c3d4e5f6", "executing", "step", step_index=0, now=1001
            )
            store.record_step("a1b2c3d4e5f6", step_index=1, step_count=2, now=1002)

            record = store.get("a1b2c3d4e5f6")

            self.assertEqual(record["state"], "executing")
            self.assertEqual(record["step_index"], 1)
            self.assertEqual(record["step_count"], 2)

            with self.assertRaisesRegex(ValueError, "步骤"):
                store.record_step("a1b2c3d4e5f6", step_index=2, step_count=2)


if __name__ == "__main__":
    unittest.main()
