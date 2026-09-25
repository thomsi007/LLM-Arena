import socket
import threading
import time
import unittest

from llm_arena.mock_server import MockLlama
from llm_arena.providers import (
    Cancelled, CancelToken, ConnectionInterrupted, EmptyResponse, EndpointUnreachable, HTTPStatusError,
    InvalidResponse, LLMConfig, LLMTimeout, ModelError, create_provider,
)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ProviderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockLlama(name="unit-model").start()

    @classmethod
    def tearDownClass(cls):
        cls.mock.stop()

    def setUp(self):
        self.mock.mode = "ok"
        self.mock.delay = 0
        self.mock.token_delay = 0

    def prov(self, **kw):
        cfg = LLMConfig.from_dict({"base_url": self.mock.url, "retries": 0, "timeout": 5, **kw}, "A")
        return create_provider(cfg)

    def test_stream_collects_tokens_and_usage(self):
        tokens = []
        r = self.prov().chat([{"role": "user", "content": "hello"}], on_token=lambda d, k: tokens.append(d))
        self.assertTrue(r.streamed)
        self.assertIn("unit-model", r.content)
        self.assertEqual("".join(t for t in tokens), r.content)
        self.assertEqual(r.model, "unit-model")
        self.assertEqual(r.prompt_tokens, 11)
        self.assertFalse(r.tokens_estimated)
        self.assertEqual(r.tokens_per_second, 42.0)

    def test_non_stream(self):
        r = self.prov(stream=False).chat([{"role": "user", "content": "hi"}])
        self.assertFalse(r.streamed)
        self.assertEqual(r.total_tokens, r.prompt_tokens + r.completion_tokens)

    def test_model_autodetect_and_v1_suffix(self):
        p = self.prov(base_url=self.mock.url + "/v1")
        self.assertEqual(p.list_models(), ["unit-model"])
        p.chat([{"role": "user", "content": "x"}])
        self.assertEqual(self.mock.requests[-1]["model"], "unit-model")

    def test_connection_check(self):
        res = self.prov().test_connection()
        self.assertTrue(res["ok"])
        self.assertEqual([s["step"] for s in res["steps"]], ["health", "models", "completion"])

    def test_loading_server_reports_not_ok(self):
        self.mock.mode = "loading"
        res = self.prov().test_connection()
        self.assertFalse(res["ok"])

    def test_http_500(self):
        self.mock.mode = "http500"
        with self.assertRaises(HTTPStatusError) as cm:
            self.prov().chat([{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.status, 500)
        self.assertTrue(cm.exception.retryable)
        self.assertIn("internal boom", cm.exception.message)

    def test_http_400_not_retryable(self):
        self.mock.mode = "http400"
        before = len(self.mock.requests)
        with self.assertRaises(HTTPStatusError) as cm:
            self.prov(retries=3).chat([{"role": "user", "content": "x"}])
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(len(self.mock.requests) - before, 1)

    def test_invalid_json(self):
        self.mock.mode = "invalid_json"
        with self.assertRaises(InvalidResponse):
            self.prov(stream=False).chat([{"role": "user", "content": "x"}])

    def test_empty_response(self):
        self.mock.mode = "empty"
        with self.assertRaises(EmptyResponse):
            self.prov().chat([{"role": "user", "content": "x"}])

    def test_interrupted_stream(self):
        self.mock.mode = "interrupt"
        with self.assertRaises(ConnectionInterrupted) as cm:
            self.prov().chat([{"role": "user", "content": "a long enough prompt to create chunks"}])
        self.assertTrue(cm.exception.partial)

    def test_model_error_in_stream_and_plain(self):
        self.mock.mode = "model_error"
        with self.assertRaises(ModelError):
            self.prov().chat([{"role": "user", "content": "some prompt text here"}])
        with self.assertRaises(ModelError):
            self.prov(stream=False).chat([{"role": "user", "content": "x"}])

    def test_timeout(self):
        self.mock.delay = 2
        with self.assertRaises(LLMTimeout):
            self.prov(timeout=1).chat([{"role": "user", "content": "x"}])

    def test_unreachable(self):
        p = create_provider(LLMConfig.from_dict({"base_url": f"http://127.0.0.1:{free_port()}", "retries": 0}, "B"))
        with self.assertRaises(EndpointUnreachable):
            p.chat([{"role": "user", "content": "x"}])
        self.assertFalse(p.test_connection()["ok"])

    def test_retry_then_success(self):
        self.mock.mode = "http500"
        resets = []
        threading.Timer(0.3, lambda: setattr(self.mock, "mode", "ok")).start()
        r = self.prov(retries=2).chat([{"role": "user", "content": "x"}], on_token=lambda d, k: resets.append(k))
        self.assertGreaterEqual(r.attempts, 2)
        self.assertIn("reset", resets)

    def test_cancel_mid_stream(self):
        self.mock.token_delay = 0.05
        tok = CancelToken()
        threading.Timer(0.2, tok.cancel).start()
        t0 = time.monotonic()
        with self.assertRaises(Cancelled):
            self.prov(timeout=30).chat([{"role": "user", "content": "x" * 400}], cancel=tok)
        self.assertLess(time.monotonic() - t0, 3)

    def test_think_tags_are_split(self):
        from llm_arena.providers.openai_compat import split_thinking
        content, think = split_thinking("<think>plan</think>Answer")
        self.assertEqual((content, think), ("Answer", "plan"))


if __name__ == "__main__":
    unittest.main()
