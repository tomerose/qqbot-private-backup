import os
import sys
import tempfile
from pathlib import Path

import unittest

_PROJ_ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = str(_PROJ_ROOT / "astrbot")

from astrbot.api.message_components import At, Plain

PLUGIN_DIR = _PROJ_ROOT / "astrbot" / "data" / "plugins" / "claude_code_agent"
sys.path.insert(0, str(PLUGIN_DIR))

from agent_core import (  # noqa: E402
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    BACKEND_WORKBUDDY,
    ApprovalRegistry,
    assess_task_risk,
    build_agent_env,
    build_backend_command,
    create_job_dir,
    build_process_tree_kill_command,
    discover_deliverables,
    build_job_agent_env,
    extract_agent_command,
    is_sensitive_deliverable,
    is_inline_media_payload,
    is_within_allowed_roots,
    normalize_backend,
    parse_backend_result,
    parse_failure,
    parse_result,
    redact_local_paths,
    redact_sensitive_text,
    referenced_workspace_files,
    upload_aiocqhttp_group_file,
    upload_aiocqhttp_private_file,
    validate_work_dir,
    validate_task,
)


class AgentCoreTests(unittest.TestCase):
    def test_job_environment_hides_github_write_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = build_job_agent_env(
                root,
                {"GH_TOKEN": "secret", "GITHUB_TOKEN": "secret2", "PATH": "safe"},
            )

            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertTrue(Path(env["GH_CONFIG_DIR"]).is_dir())
            self.assertTrue(Path(env["GH_CONFIG_DIR"]).is_relative_to(root.resolve()))

    def test_validate_task_trims_and_rejects_empty_or_oversized_input(self):
        self.assertEqual(validate_task("  写一个文件  "), "写一个文件")
        with self.assertRaisesRegex(ValueError, "不能为空"):
            validate_task("   ")
        with self.assertRaisesRegex(ValueError, "过长"):
            validate_task("x" * 3001)


    def test_create_job_dir_is_a_new_child_of_fixed_workspace(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            job_id = "a1b2c3d4e5f6"
            job_dir = create_job_dir(workspace, job_id)
            self.assertEqual(job_dir, (workspace / "jobs" / job_id).resolve())
            self.assertTrue(job_dir.is_dir())
            with self.assertRaisesRegex(ValueError, "已存在"):
                create_job_dir(workspace, job_id)
            with self.assertRaisesRegex(ValueError, "非法"):
                create_job_dir(workspace, "../escape")


    def test_normalize_backend_accepts_supported_names_and_rejects_unknown(self):
        self.assertEqual(normalize_backend("Claude"), BACKEND_CLAUDE)
        self.assertEqual(normalize_backend("codex"), BACKEND_CODEX)
        self.assertEqual(normalize_backend("workbuddy"), BACKEND_WORKBUDDY)
        with self.assertRaisesRegex(ValueError, "后端"):
            normalize_backend("shell")

    def test_claude_command_uses_full_permissions_without_global_config_changes(self):
        command = build_backend_command(BACKEND_CLAUDE, "执行任务", Path(r"D:\work"), Path(r"D:\return"))
        self.assertTrue(command[0].lower().endswith("claude.exe"))
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertIn("--allow-dangerously-skip-permissions", command)
        self.assertIn("bypassPermissions", command)
        self.assertIn("default", command)
        # ponytail: agent gets full host Claude Code capability.
        self.assertIn("--settings", command)
        self.assertIn("--append-system-prompt", command)
        self.assertIn("--add-dir", command)
        self.assertNotIn("--allowedTools", command)
        self.assertNotIn("--max-budget-usd", command)
        self.assertNotIn("--max-turns", command)
        # Task is passed via -p, not mixed with the preamble.
        task_index = command.index("执行任务")
        self.assertEqual(command[task_index - 1], "-p")

    def test_codex_and_workbuddy_commands_use_full_permission_cli_flags(self):
        cwd = Path(r"D:\project")
        output = Path(r"D:\return")
        codex = build_backend_command(BACKEND_CODEX, "执行任务", cwd, output)
        self.assertIn("exec", codex)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", codex)
        self.assertEqual(codex[codex.index("-m") + 1], "gpt-5.6-terra")
        self.assertEqual(codex[codex.index("-C") + 1], str(cwd))
        self.assertIn("--skip-git-repo-check", codex)
        workbuddy = build_backend_command(BACKEND_WORKBUDDY, "执行任务", cwd, output)
        self.assertEqual(Path(workbuddy[0]).name.lower(), "node.exe")
        self.assertIn("--dangerously-skip-permissions", workbuddy)
        self.assertIn("bypassPermissions", workbuddy)
        self.assertIn("执行任务", workbuddy[-1])

    def test_public_codex_command_uses_workspace_sandbox(self):
        command = build_backend_command(
            BACKEND_CODEX,
            "生成报告",
            Path(r"D:\job"),
            Path(r"D:\job\outputs"),
            trusted_runtime=False,
        )
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--add-dir", command)

    def test_execution_prompt_denies_unapproved_side_effects_and_never_exports_secrets(self):
        safe = build_backend_command(
            BACKEND_CLAUDE,
            "整理项目",
            Path(r"D:\work"),
            Path(r"D:\return"),
            high_risk_approved=False,
        )
        approved = build_backend_command(
            BACKEND_CLAUDE,
            "删除旧文件",
            Path(r"D:\work"),
            Path(r"D:\return"),
            high_risk_approved=True,
        )
        self.assertIn("未授权执行高风险操作", "\n".join(safe))
        self.assertIn("已获所有者二次确认", "\n".join(approved))
        self.assertIn("不得复制到交付目录", "\n".join(safe))

    def test_validate_work_dir_requires_an_existing_absolute_directory(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(validate_work_dir(tmp), Path(tmp).resolve())
        with self.assertRaisesRegex(ValueError, "绝对"):
            validate_work_dir("relative/path")
        with self.assertRaisesRegex(ValueError, "不存在"):
            validate_work_dir(r"Z:\definitely-missing-agent-dir")

    def test_parse_backend_result_handles_three_cli_envelopes(self):
        self.assertEqual(parse_backend_result(BACKEND_CLAUDE, '{"result":"完成"}'), "完成")
        self.assertEqual(parse_backend_result(BACKEND_CODEX, "Codex 完成"), "Codex 完成")
        self.assertEqual(parse_backend_result(BACKEND_WORKBUDDY, '{"result":"Buddy 完成"}'), "Buddy 完成")
        workbuddy_messages = '[{"type":"message","role":"user","content":[{"type":"input_text","text":"任务"}]},{"type":"message","role":"assistant","content":[{"type":"output_text","text":"OK"}]}]'
        self.assertEqual(parse_backend_result(BACKEND_WORKBUDDY, workbuddy_messages), "OK")

    def test_discover_deliverables_stays_inside_output_dir_and_is_bounded(self):
        import tempfile
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "outputs"
            output.mkdir()
            Image.new("RGB", (2, 2), "white").save(output / "a.png")
            (output / "b.txt").write_text("ok", encoding="utf-8")
            (output / "too-big.bin").write_bytes(b"x" * 20)
            outside = root / "secret.txt"
            outside.write_text("secret", encoding="utf-8")
            items = discover_deliverables(output, max_files=2, max_file_bytes=1000)
            self.assertEqual([(item.path.name, item.kind) for item in items], [("a.png", "image"), ("b.txt", "file")])

    def test_redact_local_paths_removes_owner_details_from_reply(self):
        text = r"文件在 `D:\Claudecoda学习\qqbot\astrbot\data\workspaces\llbot-3806573022_GroupMessage_945598390\报告.docx`，已经完成。"
        cleaned = redact_local_paths(text)
        self.assertNotIn("D:\\", cleaned)
        self.assertNotIn("Claudecoda", cleaned)
        self.assertNotIn("workspaces", cleaned)
        self.assertIn("[本机路径]", cleaned)

    def test_redact_local_paths_preserves_web_urls(self):
        url = "https://www.youtube.com/watch?v=mbappe"
        self.assertEqual(redact_local_paths(url), url)

    def test_sensitive_text_redacts_paths_and_credentials(self):
        raw = (
            r"文件在 C:\Users\liu\secret.txt，"
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz "
            "api_key=sk-abcdefghijklmnopqrstuvwxyz123456"
        )
        cleaned = redact_sensitive_text(raw)
        self.assertNotIn(r"C:\Users\liu", cleaned)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", cleaned)
        self.assertIn("[已隐藏]", cleaned)

    def test_task_risk_requires_approval_for_sensitive_actions(self):
        self.assertFalse(assess_task_risk("读取项目并新建一份代码审查报告").requires_approval)
        self.assertTrue(assess_task_risk("把结果发送到QQ群").requires_approval)
        self.assertTrue(assess_task_risk("删除旧目录并重装软件").requires_approval)
        self.assertTrue(assess_task_risk("读取浏览器 cookies 和 API key").requires_approval)
        self.assertTrue(assess_task_risk("执行 rm -rf 并关闭系统服务").requires_approval)

    def test_approval_is_one_time_expiring_and_bound_to_owner_and_chat(self):
        registry = ApprovalRegistry(ttl_seconds=300)
        pending = registry.issue(
            owner_id="1211000567",
            scope="group:123",
            task="删除旧目录",
            backend="claude",
            work_dir=Path(r"D:\work"),
            now=1000,
        )
        self.assertIsNone(registry.consume(pending.token, "other", "group:123", now=1001))
        self.assertIsNone(registry.consume(pending.token, "1211000567", "private", now=1001))
        approved = registry.consume(pending.token, "1211000567", "group:123", now=1001)
        self.assertEqual(approved.task, "删除旧目录")
        self.assertIsNone(registry.consume(pending.token, "1211000567", "group:123", now=1002))

        expired = registry.issue("1211000567", "group:123", "安装软件", "codex", Path(r"D:\work"), now=2000)
        self.assertIsNone(registry.consume(expired.token, "1211000567", "group:123", now=2301))

    def test_latest_approval_is_scoped_one_time_and_ignores_expired_entries(self):
        registry = ApprovalRegistry(ttl_seconds=300)
        registry.issue(
            "1211000567", "group:123", "删除 A", "claude", Path(r"D:\work"),
            now=1000, task_id="job1", step_digest="a" * 64,
        )
        newest = registry.issue(
            "1211000567", "group:123", "删除 B", "codex", Path(r"D:\work"),
            now=1010, task_id="job2", step_digest="b" * 64,
        )
        registry.issue(
            "1211000567", "group:999", "删除 C", "claude", Path(r"D:\work"),
            now=1020, task_id="job3", step_digest="c" * 64,
        )

        # No-task filter → multiple candidates → ambiguity guard returns None.
        self.assertIsNone(registry.consume_latest("1211000567", "group:123", now=1021))
        # Explicit task_id filter → single match → consumed.
        approved = registry.consume_latest(
            "1211000567", "group:123", now=1021,
            task_id="job2", step_digest="b" * 64,
        )
        self.assertIsNotNone(approved)
        self.assertEqual(approved.task, "删除 B")
        # One remaining candidate → no-filter OK (single candidate, unambiguous).
        older = registry.consume_latest("1211000567", "group:123", now=1022)
        self.assertIsNotNone(older)
        self.assertEqual(older.task, "删除 A")
        # Expired: nothing left.
        self.assertIsNone(registry.consume_latest("1211000567", "group:123", now=1401))

    def test_latest_approval_works_with_single_candidate_no_filter(self):
        registry = ApprovalRegistry(ttl_seconds=300)
        registry.issue("1211000567", "group:123", "删除 A", "claude", Path(r"D:\work"),
                        now=1000, task_id="job1", step_digest="a" * 64)
        # Single candidate → no-filter OK.
        approved = registry.consume_latest("1211000567", "group:123", now=1001)
        self.assertIsNotNone(approved)
        self.assertEqual(approved.task, "删除 A")

    def test_sensitive_deliverables_are_never_returned(self):
        self.assertTrue(is_sensitive_deliverable(Path("outputs/.env")))
        self.assertTrue(is_sensitive_deliverable(Path("outputs/id_rsa")))
        self.assertTrue(is_sensitive_deliverable(Path("outputs/cookies.sqlite")))
        self.assertTrue(is_sensitive_deliverable(Path("outputs/api_token_backup.txt")))
        self.assertFalse(is_sensitive_deliverable(Path("outputs/report.docx")))

    def test_delivery_discovery_blocks_sensitive_content_with_safe_filename(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = root / "outputs"
            outputs.mkdir()
            safe = outputs / "safe.txt"
            safe.write_text("three checks passed", encoding="utf-8")
            leaked = outputs / "report.txt"
            leaked.write_text("api_key=sk-abcdefghijklmnop123456", encoding="utf-8")

            discovered = discover_deliverables(outputs)
            referenced = referenced_workspace_files(root, str(leaked))

            self.assertEqual([item.path.name for item in discovered], ["safe.txt"])
            self.assertEqual(referenced, [])

    def test_local_delivery_path_must_stay_inside_an_allowed_workspace(self):
        import tempfile
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            inside = root / "image.png"
            Image.new("RGB", (2, 2), "white").save(inside)
            outside = Path(tmp) / "private.png"
            Image.new("RGB", (2, 2), "white").save(outside)
            self.assertTrue(is_within_allowed_roots(inside, [root]))
            self.assertFalse(is_within_allowed_roots(outside, [root]))

    def test_inline_image_payload_is_blocked_from_chat_delivery(self):
        self.assertTrue(is_inline_media_payload("base64://cHJpdmF0ZQ=="))
        self.assertTrue(is_inline_media_payload("data:image/png;base64,cHJpdmF0ZQ=="))
        self.assertFalse(is_inline_media_payload("https://example.com/image.png"))

    def test_referenced_workspace_files_only_returns_explicit_in_workspace_files(self):
        import tempfile
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inside = root / "answer.docx"
            with zipfile.ZipFile(inside, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("[Content_Types].xml", "<Types />")
                archive.writestr("word/document.xml", "<document>answer</document>")
            outside = root.parent / "owner-secret.txt"
            outside.write_text("secret", encoding="utf-8")
            try:
                text = f"交付文件：`{inside}`，不要发送 `{outside}`。"
                items = referenced_workspace_files(root, text, max_files=10, max_file_bytes=1000)
                self.assertEqual([item.path.name for item in items], ["answer.docx"])
            finally:
                outside.unlink(missing_ok=True)

    def test_process_tree_kill_command_uses_windows_tree_and_force_flags(self):
        self.assertEqual(
            build_process_tree_kill_command(1234),
            ["taskkill.exe", "/PID", "1234", "/T", "/F"],
        )
        with self.assertRaisesRegex(ValueError, "进程"):
            build_process_tree_kill_command(0)

    def test_group_file_delivery_uses_onebot_upload_action(self):
        import asyncio
        import tempfile

        class FakeBot:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **kwargs):
                self.calls.append((action, kwargs))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "answer.pdf"
            path.write_bytes(b"pdf")
            bot = FakeBot()
            asyncio.run(upload_aiocqhttp_group_file(bot, "945598390", path))
            self.assertEqual(len(bot.calls), 1)
            self.assertEqual(bot.calls[0][0], "upload_group_file")
            self.assertEqual(bot.calls[0][1]["group_id"], 945598390)
            self.assertEqual(bot.calls[0][1]["name"], "answer.pdf")
            self.assertEqual(
                bot.calls[0][1]["file"],
                str(path.resolve()),
            )

    def test_private_file_delivery_uses_onebot_upload_action(self):
        import asyncio
        import tempfile

        class FakeBot:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **kwargs):
                self.calls.append((action, kwargs))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "answer.pdf"
            path.write_bytes(b"pdf")
            bot = FakeBot()
            asyncio.run(upload_aiocqhttp_private_file(bot, "1211000567", path))
            self.assertEqual(len(bot.calls), 1)
            self.assertEqual(bot.calls[0][0], "upload_private_file")
            self.assertEqual(bot.calls[0][1]["user_id"], 1211000567)
            self.assertEqual(bot.calls[0][1]["name"], "answer.pdf")
            self.assertEqual(
                bot.calls[0][1]["file"],
                str(path.resolve()),
            )

    def test_group_command_requires_at_self_and_private_command_does_not(self):
        self.assertEqual(
            extract_agent_command("/agent run x", [], "3806573022", ""),
            "/agent run x",
        )
        self.assertEqual(
            extract_agent_command(
                "", [{"type": "At", "qq": "3806573022"}, {"type": "Plain", "text": "/agent run x"}], "3806573022", "123"
            ),
            "/agent run x",
        )
        self.assertEqual(
            extract_agent_command(
                "/agent run x", [{"type": "Plain", "text": "/agent run x"}], "3806573022", "123"
            ),
            "",
        )
        self.assertEqual(
            extract_agent_command(
                "",
                [At(qq="3806573022"), Plain("/agent run x")],
                "3806573022",
                "123",
            ),
            "/agent run x",
        )


    def test_parse_result_returns_result_and_hides_invalid_payload(self):
        self.assertEqual(parse_result('{"result":"完成"}'), "完成")
        self.assertEqual(parse_result("not-json"), "")

    def test_build_agent_env_preserves_current_provider_environment(self):
        env = build_agent_env(
            {
                "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                "ANTHROPIC_AUTH_TOKEN": "proxy-token",
                "ANTHROPIC_MODEL": "deepseek-v4-pro",
                "PATH": "keep",
            }
        )
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "proxy-token")
        self.assertEqual(env["ANTHROPIC_MODEL"], "deepseek-v4-pro")
        self.assertEqual(env["PATH"], "keep")

    def test_parse_failure_exposes_provider_error(self):
        raw = '{"type":"result","subtype":"error_max_budget_usd","errors":["Reached maximum budget"]}'
        self.assertEqual(parse_failure(raw), "Reached maximum budget")
        self.assertEqual(parse_failure('{"result":"完成"}'), "")


if __name__ == "__main__":
    unittest.main()
