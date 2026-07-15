import asyncio
import importlib.util
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

    def test_deep_health_checks_real_vertex_prediction_permission(self):
        async def scenario():
            generate = lambda **_kwargs: SimpleNamespace(text="OK")
            client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
            with patch.object(self.proxy.genai, "Client", return_value=client):
                result = await self.proxy.healthz(deep=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["upstream"], "ok")
            self.assertEqual(result["image_model"], "gemini-3-pro-image")

        asyncio.run(scenario())

    def test_oversized_chat_body_is_rejected_before_json_parsing(self):
        async def scenario():
            with self.assertRaises(self.proxy.RequestPayloadError) as caught:
                await self.proxy._read_json_request(
                    request_with_json({"messages": [{"role": "user", "content": "large"}]}),
                    max_bytes=8,
                )
            self.assertEqual(caught.exception.status_code, 413)

        asyncio.run(scenario())

    def test_long_chat_keeps_latest_system_and_recent_context(self):
        messages = [{"role": "system", "content": "old"}]
        messages += [
            {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)}
            for index in range(105)
        ]
        messages.insert(50, {"role": "system", "content": "current"})

        trimmed = self.proxy._validate_chat_messages(messages)

        self.assertEqual(len(trimmed), self.proxy.MAX_CHAT_MESSAGES)
        self.assertEqual(trimmed[0], {"role": "system", "content": "current"})
        self.assertEqual(trimmed[-1]["content"], "104")
        self.assertNotIn({"role": "system", "content": "old"}, trimmed)

    def test_image_generation_does_not_block_the_event_loop(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()
            order = []

            def slow_generation(_payload):
                started.set()
                release.wait(0.5)
                order.append("generated")
                return "image/png", b"png", "gemini-3.1-flash-image"

            async def release_from_event_loop():
                while not started.is_set():
                    await asyncio.sleep(0)
                order.append("event-loop")
                release.set()

            with patch.object(self.proxy, "_generate_image_sync", slow_generation):
                result, _ = await asyncio.wait_for(
                    asyncio.gather(
                        self.proxy.generate_image(request_with_json({"prompt": "cat"})),
                        release_from_event_loop(),
                    ),
                    timeout=1,
                )

            self.assertEqual(order, ["event-loop", "generated"])
            self.assertEqual(result["data"][0]["b64_json"], "cG5n")

        asyncio.run(scenario())

    def test_gif_frame_falls_back_to_secondary_image_model(self):
        calls = []

        def generate_content(**kwargs):
            calls.append((kwargs["model"], kwargs["contents"]))
            if kwargs["model"] == self.proxy.IMAGE_MODEL_PRIMARY:
                raise RuntimeError("primary unavailable")
            if kwargs["contents"] != "cat":
                return SimpleNamespace(candidates=[])
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        content=SimpleNamespace(
                            parts=[
                                SimpleNamespace(
                                    inline_data=SimpleNamespace(
                                        mime_type="image/png", data=b"fallback"
                                    )
                                )
                            ]
                        )
                    )
                ]
            )

        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
        frames = self.proxy._generate_gif_frames(client, "cat", "1:1", 1)

        self.assertEqual(frames, [b"fallback"])
        self.assertEqual(
            [model for model, _prompt in calls],
            [
                self.proxy.IMAGE_MODEL_PRIMARY,
                self.proxy.IMAGE_MODEL_FALLBACK,
                self.proxy.IMAGE_MODEL_PRIMARY,
                self.proxy.IMAGE_MODEL_FALLBACK,
            ],
        )
        self.assertNotEqual(calls[0][1], "cat")
        self.assertEqual([prompt for _model, prompt in calls[-2:]], ["cat", "cat"])

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

    def test_thinking_response_separates_thoughts_and_answer(self):
        async def scenario():
            thought = SimpleNamespace(text="分析摘要", thought=True)
            answer = SimpleNamespace(text="最终答案", thought=False)
            response = SimpleNamespace(
                text="最终答案",
                usage_metadata=None,
                candidates=[SimpleNamespace(content=SimpleNamespace(parts=[thought, answer]))],
            )
            captured = {}

            def generate_content(**kwargs):
                captured["config"] = kwargs["config"]
                return response

            client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
            with patch.object(self.proxy.genai, "Client", return_value=client):
                result = await self.proxy.chat(
                    request_with_json(
                        {
                            "model": "gemini-2.5-pro",
                            "thinking": True,
                            "custom_extra_body": {"thinking_budget": 3072},
                            "messages": [{"role": "user", "content": "test"}],
                        }
                    )
                )

            self.assertEqual(captured["config"].thinking_config.thinking_budget, 3072)
            self.assertEqual(result["thinking"]["thoughts"], "分析摘要")
            self.assertIn("最终答案", result["choices"][0]["message"]["content"])

        asyncio.run(scenario())

    def test_complex_request_leaves_thinking_to_the_vertex_model(self):
        async def scenario():
            thought = SimpleNamespace(text="内部分析", thought=True)
            answer = SimpleNamespace(text="最终答案", thought=False)
            response = SimpleNamespace(
                text="最终答案",
                usage_metadata=None,
                candidates=[SimpleNamespace(content=SimpleNamespace(parts=[thought, answer]))],
            )
            captured = {}

            def generate_content(**kwargs):
                captured["config"] = kwargs["config"]
                return response

            client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
            with patch.object(self.proxy.genai, "Client", return_value=client):
                result = await self.proxy.chat(
                    request_with_json(
                        {
                            "model": "gemini-3.5-flash",
                            "messages": [{"role": "user", "content": "帮我设计一个用户系统的架构并比较两种存储方案"}],
                        }
                    )
                )

            self.assertIsNone(captured["config"].thinking_config)
            self.assertEqual(result["choices"][0]["message"]["content"], "最终答案")
            self.assertNotIn("thinking", result)

        asyncio.run(scenario())

    def test_simple_request_does_not_set_thinking_config(self):
        async def scenario():
            captured = {}

            def generate_content(**kwargs):
                captured["config"] = kwargs["config"]
                return SimpleNamespace(text="在呀", usage_metadata=None, candidates=[])

            client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
            with patch.object(self.proxy.genai, "Client", return_value=client):
                await self.proxy.chat(
                    request_with_json(
                        {"model": "gemini-3.5-flash", "messages": [{"role": "user", "content": "在吗"}]}
                    )
                )

            self.assertIsNone(captured["config"].thinking_config)

        asyncio.run(scenario())

    def test_streaming_uses_openai_sse_chunks(self):
        async def scenario():
            async def generate_content_stream(**kwargs):
                async def chunks():
                    yield SimpleNamespace(text="hello")
                    yield SimpleNamespace(text=" world")
                return chunks()

            client = SimpleNamespace(
                aio=SimpleNamespace(
                    models=SimpleNamespace(generate_content_stream=generate_content_stream)
                )
            )
            with patch.object(self.proxy.genai, "Client", return_value=client):
                response = await self.proxy.chat(
                    request_with_json(
                        {
                            "model": "gemini-2.5-flash",
                            "stream": True,
                            "messages": [{"role": "user", "content": "test"}],
                        }
                    )
                )
                parts = [part async for part in response.body_iterator]
                body = "".join(
                    part.decode() if isinstance(part, bytes) else part for part in parts
                )

            self.assertIn('"content": "hello"', body)
            self.assertIn('"content": " world"', body)
            self.assertIn("data: [DONE]", body)

        asyncio.run(scenario())

    def test_invalid_numeric_chat_parameters_return_400(self):
        async def scenario():
            invalid_options = [
                {"max_tokens": "many"},
                {"temperature": "hot"},
                {"top_p": 2},
                {"custom_extra_body": {"thinking_budget": "deep"}},
            ]
            for options in invalid_options:
                with self.subTest(options=options):
                    result = await self.proxy.chat(
                        request_with_json(
                            {
                                "model": "gemini-2.5-pro",
                                "messages": [{"role": "user", "content": "test"}],
                                **options,
                            }
                        )
                    )
                    self.assertEqual(result.status_code, 400)
                    error = json.loads(result.body)["error"]
                    self.assertEqual(error["type"], "invalid_request_error")

        asyncio.run(scenario())

    def test_upstream_chat_error_is_logged_but_not_exposed(self):
        async def scenario():
            secret = "provider trace with project secret"
            client = SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=lambda **_kwargs: (_ for _ in ()).throw(
                        RuntimeError(secret)
                    )
                )
            )
            with patch.object(self.proxy.genai, "Client", return_value=client), self.assertLogs(
                self.proxy.logger, level="ERROR"
            ) as logs:
                result = await self.proxy.chat(
                    request_with_json(
                        {"messages": [{"role": "user", "content": "test"}]}
                    )
                )

            self.assertEqual(result.status_code, 502)
            self.assertEqual(
                json.loads(result.body)["error"]["message"],
                self.proxy.CHAT_UPSTREAM_ERROR,
            )
            self.assertNotIn(secret, result.body.decode())
            self.assertIn(secret, "\n".join(logs.output))

        asyncio.run(scenario())

    def test_rate_limited_request_retries_before_succeeding(self):
        async def scenario():
            class RateLimitError(Exception):
                status_code = 429

            calls = 0

            def generate_content(**_kwargs):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise RateLimitError("capacity exhausted")
                return SimpleNamespace(text="recovered", usage_metadata=None, candidates=[])

            client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
            with (
                patch.object(self.proxy.genai, "Client", return_value=client),
                patch.object(self.proxy.asyncio, "sleep", new_callable=AsyncMock) as sleep,
            ):
                result = await self.proxy.chat(
                    request_with_json({"messages": [{"role": "user", "content": "test"}]})
                )

            self.assertEqual(calls, 3)
            self.assertEqual(sleep.await_count, 2)
            self.assertEqual(result["choices"][0]["message"]["content"], "recovered")

        asyncio.run(scenario())

    def test_streaming_chat_error_is_not_exposed(self):
        async def scenario():
            secret = "provider streaming trace"

            async def generate_content_stream(**_kwargs):
                raise RuntimeError(secret)

            client = SimpleNamespace(
                aio=SimpleNamespace(
                    models=SimpleNamespace(
                        generate_content_stream=generate_content_stream
                    )
                )
            )
            with patch.object(self.proxy.genai, "Client", return_value=client), self.assertLogs(
                self.proxy.logger, level="ERROR"
            ):
                response = await self.proxy.chat(
                    request_with_json(
                        {
                            "stream": True,
                            "messages": [{"role": "user", "content": "test"}],
                        }
                    )
                )
                parts = [part async for part in response.body_iterator]
            body = "".join(
                part.decode() if isinstance(part, bytes) else part for part in parts
            )

            self.assertIn(self.proxy.CHAT_UPSTREAM_ERROR, body)
            self.assertNotIn(secret, body)

        asyncio.run(scenario())

    def test_empty_non_stream_response_is_an_error(self):
        async def scenario():
            response = SimpleNamespace(text="", usage_metadata=None, candidates=[])
            client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kwargs: response))
            with patch.object(self.proxy.genai, "Client", return_value=client):
                result = await self.proxy.chat(
                    request_with_json({"messages": [{"role": "user", "content": "test"}]})
                )
            self.assertEqual(result.status_code, 502)
            self.assertEqual(
                json.loads(result.body)["error"]["message"],
                self.proxy.CHAT_UPSTREAM_ERROR,
            )

        asyncio.run(scenario())

    def test_code_execution_only_response_returns_the_execution_output(self):
        async def scenario():
            response = SimpleNamespace(
                text="",
                usage_metadata=None,
                candidates=[
                    SimpleNamespace(
                        content=SimpleNamespace(
                            parts=[
                                SimpleNamespace(
                                    thought=False,
                                    text=None,
                                    code_execution_result=SimpleNamespace(output="42"),
                                )
                            ]
                        )
                    )
                ],
            )
            client = SimpleNamespace(
                models=SimpleNamespace(generate_content=lambda **_kwargs: response)
            )
            with patch.object(self.proxy.genai, "Client", return_value=client):
                result = await self.proxy.chat(
                    request_with_json(
                        {
                            "model": "gemini-3.5-flash",
                            "code_execution": True,
                            "messages": [{"role": "user", "content": "6*7"}],
                        }
                    )
                )
            self.assertEqual(result["choices"][0]["message"]["content"], "42")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
