import asyncio
import subprocess
import sys
import unittest


from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins" / "claude_code_agent"
sys.path.insert(0, str(PLUGIN_DIR))

from bounded_process_io import capture_bounded_process  # noqa: E402


async def _spawn(code: str):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class BoundedProcessIOTests(unittest.TestCase):
    def test_normal_process_returns_bounded_stdout_and_stderr(self):
        async def scenario():
            proc = await _spawn("import sys; print('ok'); print('note', file=sys.stderr)")
            result = await capture_bounded_process(
                proc, stdout_limit=1024, stderr_limit=1024, timeout=5
            )
            self.assertEqual(result.reason, "completed")
            self.assertEqual(result.returncode, 0)
            self.assertIn(b"ok", result.stdout)
            self.assertIn(b"note", result.stderr)

        asyncio.run(scenario())

    def test_stdout_limit_terminates_process_before_unbounded_capture(self):
        async def scenario():
            proc = await _spawn(
                "import sys,time; sys.stdout.write('x'*200000); sys.stdout.flush(); time.sleep(30)"
            )
            result = await capture_bounded_process(
                proc, stdout_limit=2048, stderr_limit=1024, timeout=10
            )
            self.assertEqual(result.reason, "stdout_limit")
            self.assertLessEqual(len(result.stdout), 2048)
            self.assertIsNotNone(result.returncode)

        asyncio.run(scenario())

    def test_stderr_limit_and_timeout_have_distinct_reasons(self):
        async def scenario():
            noisy = await _spawn(
                "import sys,time; sys.stderr.write('e'*100000); sys.stderr.flush(); time.sleep(30)"
            )
            limited = await capture_bounded_process(
                noisy, stdout_limit=1024, stderr_limit=1024, timeout=10
            )
            self.assertEqual(limited.reason, "stderr_limit")
            self.assertLessEqual(len(limited.stderr), 1024)

            sleepy = await _spawn("import time; time.sleep(30)")
            timed_out = await capture_bounded_process(
                sleepy, stdout_limit=1024, stderr_limit=1024, timeout=0.1
            )
            self.assertEqual(timed_out.reason, "timeout")
            self.assertIsNotNone(timed_out.returncode)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
