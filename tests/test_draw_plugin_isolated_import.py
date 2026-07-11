import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRAW_DIR = ROOT / "astrbot" / "data" / "plugins" / "draw_command"


class DrawPluginIsolatedImportTests(unittest.TestCase):
    def test_plugin_imports_without_sibling_plugins_on_sys_path(self):
        script = textwrap.dedent(
            f"""
            import importlib.util
            import sys
            import types
            from pathlib import Path

            draw_dir = Path({str(DRAW_DIR)!r})
            package = types.ModuleType("isolated_draw_command")
            package.__path__ = [str(draw_dir)]
            sys.modules[package.__name__] = package
            spec = importlib.util.spec_from_file_location(
                "isolated_draw_command.main", draw_dir / "main.py"
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            print(module.DrawCommand.__name__)
            """
        )

        result = subprocess.run(
            [sys.executable, "-I", "-c", script],
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "DrawCommand")


if __name__ == "__main__":
    unittest.main()
