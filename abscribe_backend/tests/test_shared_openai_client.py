"""Shared keepalive AsyncOpenAI: reuse one client; always aclose on exit."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from abscribe_backend.services.question_difficulty import llm_providers as lp


class _FakeCompletions:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self._replies.pop(0)),
                    finish_reason="stop",
                )
            ]
        )


def _make_fake_client_factory(tracker: dict):
    def _factory(*, max_connections: int):
        completions = _FakeCompletions([" a ", " b "])
        client = MagicMock()
        client.chat = SimpleNamespace(completions=completions)
        client.aclose = AsyncMock()
        client.close = MagicMock()
        tracker["clients"].append(client)
        tracker["max_connections"].append(max_connections)
        tracker["completions"] = completions
        return client

    return _factory


class SharedAsyncOpenAIClientTests(unittest.TestCase):
    def test_shared_client_reused_and_aclosed_on_success(self):
        tracker = {"clients": [], "max_connections": []}

        async def _run():
            async with lp.shared_async_openai_client(max_connections=7):
                a = await lp._ask_openai_async(
                    prompt="q1",
                    seed=1,
                    temperature=0.5,
                    model_name="gpt-4o",
                    system_prompt=None,
                )
                b = await lp._ask_openai_async(
                    prompt="q2",
                    seed=2,
                    temperature=0.5,
                    model_name="gpt-4o",
                    system_prompt=None,
                )
                return a, b

        with patch.object(
            lp, "_build_async_openai_client", side_effect=_make_fake_client_factory(tracker)
        ), patch.object(lp, "_LLM_WAIT_FOR_SEC", 95.0):
            a, b = asyncio.run(_run())

        self.assertEqual((a.text, b.text), ("a", "b"))
        self.assertEqual(a.finish_reason, "stop")
        self.assertEqual(len(tracker["clients"]), 1)
        self.assertEqual(tracker["max_connections"], [7])
        self.assertEqual(tracker["completions"].calls, 2)
        tracker["clients"][0].aclose.assert_awaited_once()
        self.assertIsNone(lp._async_openai_client_var.get())

    def test_shared_client_aclosed_on_error(self):
        tracker = {"clients": [], "max_connections": []}

        async def _run():
            async with lp.shared_async_openai_client(max_connections=3):
                raise RuntimeError("client abort")

        with patch.object(
            lp, "_build_async_openai_client", side_effect=_make_fake_client_factory(tracker)
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(_run())

        self.assertEqual(len(tracker["clients"]), 1)
        tracker["clients"][0].aclose.assert_awaited_once()
        self.assertIsNone(lp._async_openai_client_var.get())

    def test_fallback_client_closed_when_no_shared_context(self):
        tracker = {"clients": [], "max_connections": []}

        with patch.object(
            lp, "_build_async_openai_client", side_effect=_make_fake_client_factory(tracker)
        ), patch.object(lp, "_LLM_WAIT_FOR_SEC", 95.0):
            result = asyncio.run(
                lp._ask_openai_async(
                    prompt="q",
                    seed=1,
                    temperature=0.5,
                    model_name="gpt-4o",
                    system_prompt=None,
                )
            )

        self.assertEqual(result.text, "a")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(len(tracker["clients"]), 1)
        tracker["clients"][0].aclose.assert_awaited_once()

    def test_tcp_keepalive_options_include_so_keepalive(self):
        import socket

        opts = lp._tcp_keepalive_socket_options()
        self.assertTrue(opts)
        self.assertEqual(opts[0][:2], (socket.SOL_SOCKET, socket.SO_KEEPALIVE))
        self.assertEqual(opts[0][2], 1)


if __name__ == "__main__":
    unittest.main()
