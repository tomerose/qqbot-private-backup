import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]


def load_proxy():
    spec = importlib.util.spec_from_file_location("qqbot_gemini_proxy_tools", ROOT / "gemini-proxy.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def request_with_json(payload):
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "headers": []}, receive)


class GeminiProxyToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy = load_proxy()

    def test_installed_sdk_builds_maps_code_and_url_tools(self):
        async def scenario():
            captured = []

            def generate_content(**kwargs):
                captured.append(kwargs["config"].tools[0])
                return SimpleNamespace(text="ok", usage_metadata=None, candidates=[])

            client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
            with patch.object(self.proxy.genai, "Client", return_value=client):
                for flag in ("google_maps", "code_execution", "url_context"):
                    result = await self.proxy.chat(
                        request_with_json(
                            {
                                "model": "gemini-2.5-flash",
                                flag: True,
                                "messages": [{"role": "user", "content": "test"}],
                            }
                        )
                    )
                    self.assertEqual(result["choices"][0]["message"]["content"], "ok")
            self.assertIsNotNone(captured[0].google_maps)
            self.assertIsNotNone(captured[1].code_execution)
            self.assertIsNotNone(captured[2].url_context)

        asyncio.run(scenario())

    def test_search_allows_grounding_metadata_without_chunks(self):
        async def scenario():
            response = SimpleNamespace(
                text="grounded answer",
                usage_metadata=None,
                candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(
                    grounding_chunks=None, web_search_queries=None
                ))],
            )
            client = SimpleNamespace(
                models=SimpleNamespace(generate_content=lambda **kwargs: response)
            )
            with patch.object(self.proxy.genai, "Client", return_value=client):
                result = await self.proxy.chat(
                    request_with_json(
                        {
                            "model": "gemini-2.5-flash-search",
                            "messages": [{"role": "user", "content": "test"}],
                        }
                    )
                )
            self.assertEqual(result["grounding"]["sources"], [])
            self.assertEqual(result["choices"][0]["message"]["content"], "grounded answer")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
