#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for the async refactoring of review_contest.py.

Covers:
- _parse_retry_after edge cases
- call_model: happy path, 429/5xx retries with backoff, Retry-After,
  exhaustion, 4xx no-retry, client errors, timeout propagation
- review_unit: verdict parse, retry on garbage, Failure on unreadable files
- review_all: gathers verdicts/failures, exception containment
- coroutine signatures / concurrency clamp

Run: python3 -m unittest test_review_contest_async -v
"""

import asyncio
import inspect
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent))

import review_contest as rc
from review_contest import (
    Failure,
    ModelCallError,
    SolutionUnit,
    Verdict,
    _parse_retry_after,
    _read_sse_content,
    call_model,
    review_all,
    review_unit,
    INTER_CHUNK_TIMEOUT,
    FIRST_CHUNK_TIMEOUT,
)


# ----------------------------------------------------------------------
# Fakes for aiohttp
# ----------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for aiohttp.ClientResponse."""

    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def text(self):
        return self._body.decode("utf-8", errors="replace")

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePost:
    """Context manager returned by session.post(...), wrapping ONE response."""

    def __init__(self, response):
        self._response = response
        self.calls = 0

    async def __aenter__(self):
        self.calls += 1
        return self._response

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """
    Fake aiohttp.ClientSession.

    Each session.post() call pops exactly one queued response, like a real
    HTTP round-trip. The retry loop in call_model calls session.post() once
    per attempt, which walks the queue naturally.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if not self._responses:
            raise AssertionError(
                f"session.post() called {self.calls} times "
                "but no more responses queued"
            )
        return FakePost(self._responses.pop(0))


def good_response(content, status=200):
    return sse_response(content, status)


HANG = object()  # sentinel: readline never returns


class FakeStreamReader:
    """Отдаёт заранее заданные строки; b"" = EOF.
    Элемент HANG => зависание (для теста таймаута)."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if not self._lines:
            return b""
        item = self._lines.pop(0)
        if item is HANG:
            await asyncio.Event().wait()
        return item


class FakeStreamResponse(FakeResponse):
    """FakeResponse с .content для SSE-потока."""

    def __init__(self, status=200, lines=(), headers=None):
        super().__init__(status, b"", headers)
        self.content = FakeStreamReader(lines)


def sse_lines(*pieces, done=True):
    """Собрать SSE-линии из кусков content-дельт."""
    out = []
    for p in pieces:
        out.append(
            b"data: "
            + json.dumps({"choices": [{"delta": {"content": p}}]}).encode()
            + b"\n"
        )
        out.append(b"\n")
    if done:
        out.append(b"data: [DONE]\n")
    return out


def sse_response(content, status=200, **kw):
    """Заменяет good_response для потоковых тестов."""
    return FakeStreamResponse(status, sse_lines(content), **kw)


def make_unit(tmp: Path, alias="X", sid=123, solution="print(1)", statement="stmt"):
    base = tmp / alias
    (base / "solutions").mkdir(parents=True, exist_ok=True)
    sol = base / "solutions" / str(sid)
    sol.write_text(solution, encoding="utf-8")
    st = base / "statement.md"
    st.write_text(statement, encoding="utf-8")
    return SolutionUnit(
        alias=alias,
        solution_path=sol,
        statement_path=st,
        submission_id=sid,
        problem_id=None,
    )


def make_json_response(payload, status=200):
    return FakeResponse(status, json.dumps(payload).encode("utf-8"))


# ----------------------------------------------------------------------
# _parse_retry_after
# ----------------------------------------------------------------------


class TestParseRetryAfter(unittest.TestCase):
    def test_cases(self):
        cases = {
            None: None,
            "": None,
            "5": 5.0,
            "5.5": 5.5,
            "0": 0.0,
            "-1": None,
            "999": None,  # exceeds RETRY_AFTER_CAP=60
            "not-a-number": None,
            "Wed, 21 Oct 2026": None,  # HTTP-date unsupported
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(_parse_retry_after(value), expected)

    def test_boundary_cap(self):
        self.assertEqual(_parse_retry_after("60"), 60.0)
        self.assertIsNone(_parse_retry_after("60.001"))
        self.assertIsNone(_parse_retry_after("61"))

    def test_whitespace(self):
        self.assertEqual(_parse_retry_after("  5  "), 5.0)


# ----------------------------------------------------------------------
# call_model
# ----------------------------------------------------------------------


class TestCallModel(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = FakeSession([])
        self.sem = asyncio.Semaphore(1)
        self.kwargs = dict(
            session=self.session,
            semaphore=self.sem,
            api_key="k",
            base_url="https://api.example/v1",
            model_id="m",
            system_prompt="sys",
            user_prompt="user",
        )

    async def test_is_coroutine(self):
        self.assertTrue(inspect.iscoroutinefunction(call_model))
        self.assertTrue(inspect.iscoroutinefunction(review_unit))
        self.assertTrue(inspect.iscoroutinefunction(review_all))

    async def test_happy_path(self):
        self.session._responses = [good_response("hello world")]
        text = await call_model(**self.kwargs)
        self.assertEqual(text, "hello world")
        self.assertEqual(self.session.calls, 1)

    async def test_retry_429_then_success(self):
        self.session._responses = [
            FakeResponse(429, b"rate limited"),
            good_response("ok"),
        ]
        with patch.object(rc.asyncio, "sleep", new=AsyncMock()) as sleep:
            text = await call_model(**self.kwargs, max_retries=3, retry_delay=1.0)
        self.assertEqual(text, "ok")
        self.assertEqual(self.session.calls, 2)
        sleep.assert_awaited_once_with(1.0)

    async def test_retry_500_then_success(self):
        self.session._responses = [
            FakeResponse(503, b"unavailable"),
            good_response("ok"),
        ]
        with patch.object(rc.asyncio, "sleep", new=AsyncMock()) as sleep:
            text = await call_model(**self.kwargs, max_retries=3, retry_delay=1.0)
        self.assertEqual(text, "ok")
        sleep.assert_awaited_once_with(1.0)

    async def test_exponential_backoff_429(self):
        self.session._responses = [
            FakeResponse(429, b"a"),
            FakeResponse(429, b"b"),
            FakeResponse(429, b"c"),
        ]
        with patch.object(rc.asyncio, "sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(ModelCallError) as ctx:
                await call_model(**self.kwargs, max_retries=2, retry_delay=1.0)
        self.assertIn("429", str(ctx.exception))
        self.assertEqual(self.session.calls, 3)  # max_retries+1 requests
        delays = [c.args[0] for c in sleep.await_args_list]
        self.assertEqual(delays, [1.0, 2.0])  # 1с → 2с

    async def test_retry_after_respected(self):
        self.session._responses = [
            FakeResponse(429, b"a", headers={"Retry-After": "10"}),
            good_response("ok"),
        ]
        with patch.object(rc.asyncio, "sleep", new=AsyncMock()) as sleep:
            text = await call_model(**self.kwargs, max_retries=3, retry_delay=1.0)
        self.assertEqual(text, "ok")
        sleep.assert_awaited_once_with(10.0)  # max(1.0, 10.0)

    async def test_retry_after_over_cap_uses_backoff(self):
        self.session._responses = [
            FakeResponse(429, b"a", headers={"Retry-After": "999"}),
            good_response("ok"),
        ]
        with patch.object(rc.asyncio, "sleep", new=AsyncMock()) as sleep:
            text = await call_model(**self.kwargs, max_retries=3, retry_delay=1.0)
        self.assertEqual(text, "ok")
        sleep.assert_awaited_once_with(1.0)  # cap -> None -> backoff

    async def test_4xx_no_retry(self):
        self.session._responses = [
            FakeResponse(400, b"bad request"),
            good_response("ok"),  # must NOT be reached
        ]
        with patch.object(rc.asyncio, "sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(ModelCallError) as ctx:
                await call_model(**self.kwargs, max_retries=3)
        self.assertIn("400", str(ctx.exception))
        self.assertEqual(self.session.calls, 1)
        sleep.assert_not_awaited()

    async def test_client_error_wrapped(self):
        class Boom(rc.aiohttp.ClientError):
            pass

        class FailingPost(FakePost):
            async def __aenter__(self):
                raise Boom("conn refused")

        class FailingSession(FakeSession):
            def post(self, *args, **kwargs):
                return FailingPost(self._responses)

        sess = FailingSession([])
        kwargs = dict(self.kwargs, session=sess)
        with self.assertRaises(ModelCallError) as ctx:
            await call_model(**kwargs)
        self.assertIn("Ошибка сети", str(ctx.exception))

    async def test_timeout_propagates(self):
        class TimeoutPost(FakePost):
            async def __aenter__(self):
                raise TimeoutError("timed out")

        class TimeoutSession(FakeSession):
            def post(self, *args, **kwargs):
                return TimeoutPost(self._responses)

        sess = TimeoutSession([])
        kwargs = dict(self.kwargs, session=sess)
        with self.assertRaises(TimeoutError):
            await call_model(**kwargs)

    async def test_non_sse_body_raises(self):
        """Non-SSE 200 body results in empty content -> ModelCallError."""
        lines = [b"not-json\n"]
        self.session._responses = [FakeStreamResponse(200, lines)]
        with self.assertRaises(ModelCallError) as ctx:
            await call_model(**self.kwargs)
        self.assertIn("Ответ модели пуст", str(ctx.exception))

    async def test_stream_inband_error_raises(self):
        """data: {"error":"model overloaded"} -> ModelCallError."""
        lines = [b'data: {"error":"model overloaded"}\n', b'\n']
        self.session._responses = [FakeStreamResponse(200, lines)]
        with self.assertRaises(ModelCallError) as ctx:
            await call_model(**self.kwargs)
        self.assertIn("overloaded", str(ctx.exception))

    async def test_base_url_trailing_slash(self):
        # record the url passed to session.post
        urls = []

        class CaptureSession(FakeSession):
            def post(self, url, *args, **kwargs):
                urls.append(url)
                return FakePost(self._responses.pop(0))

        sess = CaptureSession([good_response("ok")])
        kwargs = dict(
            self.kwargs, session=sess,
            base_url="https://api.example/v1/",
        )
        await call_model(**kwargs)
        self.assertEqual(urls, ["https://api.example/v1/chat/completions"])

    async def test_semaphore_bounds_concurrency(self):
        """With a semaphore of 1, only one request is in flight at a time."""
        in_flight = [0]
        max_in_flight = [0]
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingStreamResponse(FakeStreamResponse):
            def __init__(self):
                super().__init__(status=200, lines=sse_lines("x"))

            async def __aenter__(self):
                in_flight[0] += 1
                max_in_flight[0] = max(max_in_flight[0], in_flight[0])
                entered.set()
                await release.wait()
                return self

            async def __aexit__(self, *exc):
                in_flight[0] -= 1
                return False

        class BlockingSession(FakeSession):
            def post(self, *args, **kwargs):
                return BlockingStreamResponse()

        sess = BlockingSession([])
        sem = asyncio.Semaphore(1)
        tasks = [
            asyncio.ensure_future(
                call_model(
                    session=sess, semaphore=sem, api_key="k",
                    base_url="https://api.example/v1", model_id="m",
                    system_prompt="s", user_prompt="u",
                )
            )
            for _ in range(3)
        ]
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.sleep(0.05)
        self.assertEqual(max_in_flight[0], 1)  # second task blocked by semaphore
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(max_in_flight[0], 1)

    async def test_stream_accumulates_deltas(self):
        """Content split across multiple SSE frames is concatenated."""
        lines = sse_lines('{"per', 'cent": 80, "su', 'mmary": "Good", "remarks": []}', done=True)
        self.session._responses = [FakeStreamResponse(200, lines)]
        text = await call_model(**self.kwargs)
        self.assertEqual(text, '{"percent": 80, "summary": "Good", "remarks": []}')

    async def test_stream_payload_has_stream_true(self):
        """Request body must contain stream: True and Accept: text/event-stream."""
        posted = []

        class CaptureSession(FakeSession):
            def post(self, url, *args, **kwargs):
                posted.append(kwargs)
                return FakePost(self._responses.pop(0))

        sess = CaptureSession([sse_response("ok")])
        kwargs = dict(self.kwargs, session=sess)
        await call_model(**kwargs)
        body = json.loads(posted[0]["data"])
        self.assertTrue(body.get("stream"))
        self.assertEqual(posted[0]["headers"]["Accept"], "text/event-stream")

    async def test_stream_options_cannot_disable_stream(self):
        """Even if MODEL_OPTIONS has stream: False, payload must have stream: True."""
        posted = []

        class CaptureSession(FakeSession):
            def post(self, url, *args, **kwargs):
                posted.append(kwargs)
                return FakePost(self._responses.pop(0))

        sess = CaptureSession([sse_response("ok")])
        kwargs = dict(self.kwargs, session=sess)
        with patch.object(rc, "MODEL_OPTIONS", {"stream": False, "temperature": 0}):
            await call_model(**kwargs)
        body = json.loads(posted[0]["data"])
        self.assertTrue(body.get("stream"))

    async def test_stream_done_marker_stops_read(self):
        """data: [DONE] stops reading; extra lines after it are not consumed."""
        extra_line = b'data: {"choices":[{"delta":{"content":"EXTRA"}}]}\n'
        lines = sse_lines("hello", done=True) + [extra_line, b"\n"]
        self.session._responses = [FakeStreamResponse(200, lines)]
        text = await call_model(**self.kwargs)
        self.assertEqual(text, "hello")

    async def test_stream_eof_without_done(self):
        """EOF without [DONE] terminates reading (regression guard for hot loop)."""
        lines = sse_lines("partial", done=False)  # no [DONE]
        self.session._responses = [FakeStreamResponse(200, lines)]
        text = await call_model(**self.kwargs)
        self.assertEqual(text, "partial")

    async def test_stream_ignores_keepalive_and_comments(self):
        """SSE comments and empty lines are skipped; content intact."""
        lines = [
            b": keepalive\n",
            b"\n",
            b"event: chunk\n",
            b"\n",
        ] + sse_lines("payload", done=True)
        self.session._responses = [FakeStreamResponse(200, lines)]
        text = await call_model(**self.kwargs)
        self.assertEqual(text, "payload")

    async def test_stream_ignores_null_content_and_reasoning(self):
        """delta.content=null is skipped, but delta.reasoning_content IS
        accumulated into streams (all string keys are collected).
        reasoning_content still counts as liveness (got_line)."""
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n',
            b'\n',
            b'data: {"choices":[{"delta":{"content":null}}]}\n',
            b'\n',
        ] + sse_lines("real content", done=True)
        self.session._responses = [FakeStreamResponse(200, lines)]
        text = await call_model(**self.kwargs)
        # reasoning_content not included in text output
        self.assertEqual(text, "real content")

    async def test_stream_inband_error_raises(self):
        """data: {"error":"overloaded"} -> ModelCallError."""
        lines = [b'data: {"error":"overloaded"}\n', b'\n']
        self.session._responses = [FakeStreamResponse(200, lines)]
        with self.assertRaises(ModelCallError) as ctx:
            await call_model(**self.kwargs)
        self.assertIn("overloaded", str(ctx.exception))

    async def test_stream_skips_corrupt_frame(self):
        """A corrupt data: line between good frames doesn't break the stream."""
        lines = sse_lines("before", done=False)
        lines += [
            b"data: {corrupt json\n",
            b"\n",
        ]
        lines += sse_lines("after", done=True)
        self.session._responses = [FakeStreamResponse(200, lines)]
        text = await call_model(**self.kwargs)
        self.assertEqual(text, "beforeafter")

    async def test_stream_multiple_choices_accumulated(self):
        """When choices contains multiple elements, content from all is accumulated."""
        lines = [
            b'data: {"choices":['
            b'{"delta":{"content":"A"}},'
            b'{"delta":{"content":"B"}}'
            b']}\n',
            b'\n',
        ] + sse_lines("C", done=True)
        self.session._responses = [FakeStreamResponse(200, lines)]
        text = await call_model(**self.kwargs)
        self.assertEqual(text, "ABC")

    async def test_stream_empty_content_raises(self):
        """Complete stream with zero content deltas -> ModelCallError."""
        lines = [
            b'data: {"choices":[{"delta":{"content":null}}]}\n',
            b'\n',
            b"data: [DONE]\n",
        ]
        self.session._responses = [FakeStreamResponse(200, lines)]
        with self.assertRaises(ModelCallError) as ctx:
            await call_model(**self.kwargs)
        self.assertIn("Ответ модели пуст", str(ctx.exception))
        # No non-empty streams received -> streams attribute is an empty dict
        self.assertEqual(ctx.exception.streams, {})

    async def test_stream_empty_content_with_other_streams_details(self):
        """Stream with no 'content' but other stream keys -> empty error raised."""
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n',
            b'\n',
            b"data: [DONE]\n",
        ]
        self.session._responses = [FakeStreamResponse(200, lines)]
        with self.assertRaises(ModelCallError) as ctx:
            await call_model(**self.kwargs)
        self.assertIn("Ответ модели пуст", str(ctx.exception))
        # Reasoning streams are attached to the error
        self.assertIsNotNone(ctx.exception.streams)
        self.assertIn("reasoning_content", ctx.exception.streams)
        self.assertEqual(ctx.exception.streams["reasoning_content"], "thinking...")

    async def test_stream_multi_key_delta(self):
        """A delta with content and reasoning_content accumulates both keys."""
        lines = [
            b'data: {"choices":[{"delta":{"content":"A",'
            b'"reasoning_content":"think"}}]}\n',
            b'\n',
            b'data: {"choices":[{"delta":{"content":"B",'
            b'"reasoning_content":"-ing"}}]}\n',
            b'\n',
            b"data: [DONE]\n",
        ]
        self.session._responses = [FakeStreamResponse(200, lines)]
        text = await call_model(**self.kwargs)
        self.assertEqual(text, "AB")

    async def test_stream_only_non_content_key(self):
        """Delta with only a non-content key -> content empty, error raised."""
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n',
            b'\n',
            b"data: [DONE]\n",
        ]
        self.session._responses = [FakeStreamResponse(200, lines)]
        with self.assertRaises(ModelCallError) as ctx:
            await call_model(**self.kwargs)
        self.assertIn("Ответ модели пуст", str(ctx.exception))
        # The reasoning stream is attached to the error
        self.assertIsNotNone(ctx.exception.streams)
        self.assertIn("reasoning_content", ctx.exception.streams)

    async def test_model_call_error_streams_attribute(self):
        """ModelCallError carries the accumulated streams on empty content."""
        # Empty content WITH reasoning streams -> streams dict populated
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n',
            b'\n',
            b"data: [DONE]\n",
        ]
        self.session._responses = [FakeStreamResponse(200, lines)]
        with self.assertRaises(ModelCallError) as ctx:
            await call_model(**self.kwargs)
        self.assertEqual(
            ctx.exception.streams, {"reasoning_content": "think"}
        )

        # Empty content with NO non-content streams -> empty dict
        lines2 = [
            b'data: {"choices":[{"delta":{"content":null}}]}\n',
            b'\n',
            b"data: [DONE]\n",
        ]
        self.session._responses = [FakeStreamResponse(200, lines2)]
        with self.assertRaises(ModelCallError) as ctx:
            await call_model(**self.kwargs)
        self.assertEqual(ctx.exception.streams, {})

    async def test_reasoning_debug_false_does_not_print_reasoning(self):
        """reasoning_debug=False: reasoning accumulated but never printed."""
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n',
            b'\n',
        ] + sse_lines("real content", done=True)
        self.session._responses = [FakeStreamResponse(200, lines)]
        with patch.object(rc, "print") as mock_print:
            text = await call_model(**self.kwargs)
        self.assertEqual(text, "real content")
        # reasoning_content must never appear in print output
        for call in mock_print.call_args_list:
            args = call.args
            if args and "reasoning_content" in str(args[0]):
                self.fail(f"reasoning leaked into print: {args[0]!r}")

    async def test_reasoning_debug_true_prints_reasoning(self):
        """reasoning_debug=True: reasoning_content is printed."""
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n',
            b'\n',
        ] + sse_lines("real content", done=True)
        self.session._responses = [FakeStreamResponse(200, lines)]
        with patch.object(rc, "print") as mock_print:
            await call_model(**self.kwargs, reasoning_debug=True)
        printed = " ".join(
            str(c.args[0]) for c in mock_print.call_args_list if c.args
        )
        self.assertIn("reasoning_content", printed)

    async def test_read_sse_content_returns_tuple(self):
        """_read_sse_content returns (content, streams)."""
        # reasoning_debug=False: streams returned intact
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n',
            b'\n',
        ] + sse_lines("hello", done=True)
        content, streams = await _read_sse_content(
            FakeStreamResponse(200, lines), 0.1, 0.1, None,
        )
        self.assertEqual(content, "hello")
        self.assertIsInstance(streams, dict)
        self.assertEqual(streams["reasoning_content"], "think")
        self.assertEqual(streams["content"], "hello")

        # reasoning_debug=True: streams is None (consumed by printing tails)
        lines2 = [
            b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n',
            b'\n',
        ] + sse_lines("hello", done=True)
        with patch.object(rc, "print"):
            content2, streams2 = await _read_sse_content(
                FakeStreamResponse(200, lines2), 0.1, 0.1, None,
                reasoning_debug=True,
            )
        self.assertEqual(content2, "hello")
        self.assertIsNone(streams2)

    async def test_stream_message_instead_of_delta(self):
        """Providers may use 'message' instead of 'delta'; content accumulates."""
        lines = [
            b'data: {"choices":[{"message":{"content":"hello"}}]}\n',
            b'\n',
            b"data: [DONE]\n",
        ]
        self.session._responses = [FakeStreamResponse(200, lines)]
        text = await call_model(**self.kwargs)
        self.assertEqual(text, "hello")

    async def test_inter_chunk_timeout_raises(self):
        """If no chunk arrives within inter_chunk_timeout, TimeoutError is raised."""
        lines = sse_lines("first", done=False) + [HANG]
        self.session._responses = [FakeStreamResponse(200, lines)]
        kwargs = dict(self.kwargs, timeout=0.1)
        with patch.object(rc, "FIRST_CHUNK_TIMEOUT", 0.1):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(call_model(**kwargs), timeout=2.0)

    async def test_first_chunk_budget_longer_than_inter(self):
        """First-chunk timeout is longer than inter-chunk timeout."""
        lines = [HANG]  # never sends first chunk
        self.session._responses = [FakeStreamResponse(200, lines)]
        kwargs = dict(self.kwargs, timeout=0.1)
        with patch.object(rc, "FIRST_CHUNK_TIMEOUT", 0.5):
            start = asyncio.get_event_loop().time()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(call_model(**kwargs), timeout=5.0)
            elapsed = asyncio.get_event_loop().time() - start
            # Should fail near FIRST_CHUNK_TIMEOUT (0.5s), not inter_chunk (0.1s)
            self.assertGreater(elapsed, 0.3)

    async def test_first_chunk_budget_survives_keepalive(self):
        """SSE comment lines (no choices) should not shorten first-chunk budget."""
        lines = [b": keepalive\n", b"\n", HANG]
        self.session._responses = [FakeStreamResponse(200, lines)]
        kwargs = dict(self.kwargs, timeout=0.1)
        with patch.object(rc, "FIRST_CHUNK_TIMEOUT", 0.5):
            start = asyncio.get_event_loop().time()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(call_model(**kwargs), timeout=5.0)
            elapsed = asyncio.get_event_loop().time() - start
            # Should timeout near 0.5s (first_chunk), not 0.1s (inter_chunk)
            # because keepalive/comments have no choices and don't flip got_line
            self.assertGreater(elapsed, 0.3)

    async def test_reasoning_delta_switches_to_inter_chunk_budget(self):
        """A reasoning_content delta (model is thinking) confirms liveness
        and switches from first_chunk to inter_chunk timeout."""
        # reasoning delta arrives, then the stream hangs.
        # With FIRST_CHUNK_TIMEOUT=0.5 and inter_chunk=0.1,
        # the reasoning delta should switch the budget to 0.1s,
        # so the timeout fires near 0.1s after the reasoning delta, not 0.5s.
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n',
            b'\n',
            HANG,
        ]
        self.session._responses = [FakeStreamResponse(200, lines)]
        kwargs = dict(self.kwargs, timeout=0.1)
        with patch.object(rc, "FIRST_CHUNK_TIMEOUT", 0.5):
            start = asyncio.get_event_loop().time()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(call_model(**kwargs), timeout=5.0)
            elapsed = asyncio.get_event_loop().time() - start
            # Reasoning delta confirmed liveness -> budget switched to inter_chunk (0.1s).
            # Total time ≈ time_to_reasoning_delta + 0.1s, well under 0.5s.
            self.assertLess(elapsed, 0.45)

    async def test_max_stream_chars_guard(self):
        """Stream exceeding MAX_STREAM_CHARS -> ModelCallError."""
        huge = "x" * 500
        lines = sse_lines(huge, done=False) * 5  # 5 * 500 = 2500
        lines.append(b"data: [DONE]\n")
        self.session._responses = [FakeStreamResponse(200, lines)]
        with patch.object(rc, "MAX_STREAM_CHARS", 1000):
            with self.assertRaises(ModelCallError) as ctx:
                await call_model(**self.kwargs)
        self.assertIn("превысил", str(ctx.exception))

    async def test_cancel_between_chunks_returns_empty(self):
        """When cancel is set during streaming, call_model returns empty string."""
        cancel = asyncio.Event()
        lines = list(sse_lines("first", done=False))
        resp = FakeStreamResponse(200, lines)
        original_readline = resp.content.readline

        async def delayed_readline():
            # Позволяем cancel.set() cработать, пока читается первый чанк:
            # проверка cancel в _read_sse_content происходит на верхушке цикла,
            # поэтому cancel должен быть установлен к моменту завершения чтения.
            await asyncio.sleep(0.15)
            return await original_readline()

        resp.content.readline = delayed_readline
        self.session._responses = [resp]
        kwargs = dict(self.kwargs, cancel=cancel)

        async def set_cancel():
            await asyncio.sleep(0.05)
            cancel.set()

        asyncio.create_task(set_cancel())
        text = await asyncio.wait_for(call_model(**kwargs), timeout=5.0)
        self.assertEqual(text, "")


# ----------------------------------------------------------------------
# review_unit
# ----------------------------------------------------------------------


class TestReviewUnit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.unit = make_unit(self.base)
        self.sem = asyncio.Semaphore(1)

    async def test_verdict_parsed(self):
        payload = {"percent": 80, "summary": "Good", "remarks": ["a", "b"]}
        with patch.object(
            rc, "call_model",
            new=AsyncMock(return_value=json.dumps(payload)),
        ):
            result = await review_unit(
                self.unit, session=None, semaphore=self.sem,
                api_key="k", base_url="u", model_id="m", system_prompt="s",
            )
        self.assertIsInstance(result, Verdict)
        self.assertEqual(result.percent, 80.0)
        self.assertEqual(result.summary, "Good")
        self.assertEqual(result.remarks, ["a", "b"])
        self.assertEqual(result.attempts, 1)

    async def test_garbage_retries_then_failure(self):
        with patch.object(rc, "call_model", new=AsyncMock(return_value="no json here")), \
             patch.object(rc.asyncio, "sleep", new=AsyncMock()):
            result = await review_unit(
                self.unit, session=None, semaphore=self.sem,
                api_key="k", base_url="u", model_id="m", system_prompt="s",
                max_attempts=3,
            )
        self.assertIsInstance(result, Failure)
        self.assertIn("не удалось получить разбираемый вердикт", result.reason)

    async def test_call_model_error_retries(self):
        mock_call = AsyncMock(side_effect=ModelCallError("HTTP 500"))
        with patch.object(rc, "call_model", new=mock_call), \
             patch.object(rc.asyncio, "sleep", new=AsyncMock()):
            result = await review_unit(
                self.unit, session=None, semaphore=self.sem,
                api_key="k", base_url="u", model_id="m", system_prompt="s",
                max_attempts=2,
            )
        self.assertIsInstance(result, Failure)
        self.assertEqual(mock_call.await_count, 2)

    async def test_unreadable_files_failure(self):
        unit = SolutionUnit(
            alias="X", submission_id=1, problem_id=None,
            solution_path=self.base / "missing" / "1",
            statement_path=self.base / "missing" / "statement.md",
        )
        result = await review_unit(
            unit, session=None, semaphore=self.sem,
            api_key="k", base_url="u", model_id="m", system_prompt="s",
        )
        self.assertIsInstance(result, Failure)
        self.assertIn("не удалось прочитать файлы решения", result.reason)

    async def test_cancel_mid_stream_is_failure(self):
        """When call_model returns empty due to cancel, review_unit returns Failure."""
        unit = make_unit(self.base)
        cancel = asyncio.Event()
        cancel.set()
        with patch.object(rc, "call_model", new=AsyncMock(return_value="")):
            result = await review_unit(
                unit, session=None, semaphore=self.sem,
                api_key="k", base_url="u", model_id="m", system_prompt="s",
                cancel=cancel,
            )
        self.assertIsInstance(result, Failure)
        self.assertIn(rc.CANCEL_REASON, result.reason)

    async def test_call_model_error_prints_streams(self):
        """On ModelCallError with streams, review_unit dumps them as logs."""
        err = ModelCallError("Ответ модели пуст", streams={
            "reasoning_content": "thinking...",
        })
        with patch.object(
            rc, "call_model",
            new=AsyncMock(side_effect=err),
        ), patch.object(rc.asyncio, "sleep", new=AsyncMock()), \
                patch.object(rc, "print") as mock_print:
            result = await review_unit(
                self.unit, session=None, semaphore=self.sem,
                api_key="k", base_url="u", model_id="m", system_prompt="s",
                max_attempts=1,
            )
        self.assertIsInstance(result, Failure)
        # The --- Лог --- / value / --- Конец лога --- block was printed.
        # Note: the print format uses f"\n--- Лог {key} ---" (leading \n).
        printed = "".join(
            str(c.args[0]) for c in mock_print.call_args_list if c.args
        )
        self.assertIn("--- Лог reasoning_content ---", printed)
        self.assertIn("thinking...", printed)
        self.assertIn("--- Конец лога ---", printed)

    async def test_call_model_error_no_streams_no_logs(self):
        """On ModelCallError with streams=None, no --- Лог --- block is printed."""
        err = ModelCallError("Ответ модели пуст")  # streams defaults to None
        with patch.object(
            rc, "call_model",
            new=AsyncMock(side_effect=err),
        ), patch.object(rc.asyncio, "sleep", new=AsyncMock()), \
                patch.object(rc, "print") as mock_print:
            result = await review_unit(
                self.unit, session=None, semaphore=self.sem,
                api_key="k", base_url="u", model_id="m", system_prompt="s",
                max_attempts=1,
            )
        self.assertIsInstance(result, Failure)
        for call in mock_print.call_args_list:
            args = call.args
            if args and "Конец лога" in str(args[0]):
                self.fail("--- Лог --- block printed despite streams=None")


# ----------------------------------------------------------------------
# review_all
# ----------------------------------------------------------------------


class TestReviewAll(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.units = [
            make_unit(self.base, alias="A", sid=1, statement="s1"),
            make_unit(self.base, alias="B", sid=2, statement="s2"),
        ]

    async def test_verdicts_and_failures_split(self):
        payload = {"percent": 65, "summary": "ok", "remarks": []}

        async def fake_review_unit(unit, session, semaphore, api_key,
                                   base_url, model_id, system_prompt, **kwargs):
            if unit.alias == "A":
                parsed = rc.validate_payload(payload)
                return rc._make_verdict(unit, parsed, 1)
            return Failure(
                alias=unit.alias, submission_id=unit.submission_id,
                path=unit.solution_path, reason="boom",
            )

        with patch.object(rc, "review_unit", new=fake_review_unit):
            verdicts, failures = await review_all(
                self.units, api_key="k", base_url="u",
                model_id="m", system_prompt="s", concurrency=1,
            )
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(verdicts[0].unit.alias, "A")
        self.assertEqual(failures[0].reason, "boom")

    async def test_unexpected_exception_contained(self):
        async def boom(unit, *args, **kwargs):
            raise RuntimeError("unexpected")

        with patch.object(rc, "review_unit", new=boom):
            verdicts, failures = await review_all(
                self.units, api_key="k", base_url="u",
                model_id="m", system_prompt="s",
            )
        self.assertEqual(len(verdicts), 0)
        self.assertEqual(len(failures), 2)
        for failure in failures:
            self.assertIn("внутренняя ошибка", failure.reason)
            self.assertIn("RuntimeError", failure.reason)

    async def test_concurrent_progress_lines(self):
        """All units are reviewed; ordering of completion messages is fine."""
        payload = {"percent": 50, "summary": "s", "remarks": []}

        async def fake_review_unit(unit, *args, **kwargs):
            await asyncio.sleep(0.01)
            parsed = rc.validate_payload(payload)
            return rc._make_verdict(unit, parsed, 1)

        with patch.object(rc, "review_unit", new=fake_review_unit):
            verdicts, failures = await review_all(
                self.units, api_key="k", base_url="u",
                model_id="m", system_prompt="s", concurrency=2,
            )
        self.assertEqual(len(verdicts), 2)
        self.assertEqual(len(failures), 0)


# ----------------------------------------------------------------------
# main() plumbing (async entry)
# ----------------------------------------------------------------------


class TestMain(unittest.IsolatedAsyncioTestCase):
    async def test_main_is_coroutine(self):
        self.assertTrue(inspect.iscoroutinefunction(rc.main))

    async def test_main_requires_api_key(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(rc.sys, "exit", side_effect=SystemExit(1)) as exit_, \
             patch.object(rc.sys, "argv", ["review_contest.py", "99999", "--dry-run"]):
            with self.assertRaises(SystemExit) as ctx:
                await rc.main()
        self.assertEqual(ctx.exception.code, 1)
        exit_.assert_called_once_with(1)


# ----------------------------------------------------------------------
# cancel_event / Ctrl+C handling
# ----------------------------------------------------------------------


class TestCancelEvent(unittest.IsolatedAsyncioTestCase):
    """Tests for the cancel event propagation through review_unit and review_all."""

    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    async def test_review_unit_cancel_before_first_attempt(self):
        """If cancel is set before review_unit starts, it returns Failure immediately."""
        unit = make_unit(self.base, alias="X", sid=1)
        cancel = asyncio.Event()
        cancel.set()

        with patch.object(rc, "call_model", new=AsyncMock()) as mock_call:
            result = await review_unit(
                unit, session=None, semaphore=asyncio.Semaphore(1),
                api_key="k", base_url="u", model_id="m", system_prompt="s",
                cancel=cancel,
            )

        self.assertIsInstance(result, Failure)
        self.assertIn(rc.CANCEL_REASON, result.reason)
        mock_call.assert_not_awaited()

    async def test_review_unit_cancel_between_attempts(self):
        """If cancel is set after a failed attempt, review_unit
        returns Failure instead of retrying."""
        unit = make_unit(self.base, alias="X", sid=1)
        cancel = asyncio.Event()

        # First call returns garbage (no parseable JSON), then cancel is set
        mock_call = AsyncMock(return_value="not json")
        with patch.object(rc, "call_model", new=mock_call), \
             patch.object(rc.asyncio, "sleep", new=AsyncMock()):
            # Set cancel after the first attempt
            original_call_model = mock_call

            async def call_and_cancel(*args, **kwargs):
                result = await original_call_model(*args, **kwargs)
                cancel.set()
                return result

            # Actually, let's set cancel from the sleep mock
            async def sleep_and_cancel(delay):
                cancel.set()

            with patch.object(rc, "call_model", new=AsyncMock(return_value="not json")), \
                 patch.object(rc.asyncio, "sleep", new=AsyncMock(side_effect=sleep_and_cancel)):
                result = await review_unit(
                    unit, session=None, semaphore=asyncio.Semaphore(1),
                    api_key="k", base_url="u", model_id="m", system_prompt="s",
                    max_attempts=3,
                    cancel=cancel,
                )

        self.assertIsInstance(result, Failure)
        self.assertIn(rc.CANCEL_REASON, result.reason)

    async def test_review_unit_cancel_none_works_normally(self):
        """cancel=None works exactly like before (no cancel check)."""
        unit = make_unit(self.base, alias="X", sid=1)
        payload = {"percent": 80, "summary": "Good", "remarks": []}
        with patch.object(
            rc, "call_model",
            new=AsyncMock(return_value=json.dumps(payload)),
        ):
            result = await review_unit(
                unit, session=None, semaphore=asyncio.Semaphore(1),
                api_key="k", base_url="u", model_id="m", system_prompt="s",
                cancel=None,
            )
        self.assertIsInstance(result, Verdict)
        self.assertEqual(result.percent, 80.0)

    async def test_review_all_cancel_skips_pending(self):
        """When cancel is set, review_all returns partial results
        and marks pending units as cancelled."""
        units = [
            make_unit(self.base, alias="A", sid=i, statement=f"s{i}")
            for i in range(1, 5)
        ]
        cancel = asyncio.Event()

        # With concurrency=2, first two tasks start concurrently.
        # They succeed before cancel is set. After they complete,
        # remaining tasks see cancel and return Failure.
        done_count = 0

        async def controlled_review_unit(unit, *args, **kwargs):
            nonlocal done_count
            done_count += 1
            # First 2 tasks succeed (before cancel is set)
            if done_count <= 2:
                parsed = rc.validate_payload(
                    {"percent": 70, "summary": "ok", "remarks": []}
                )
                result = rc._make_verdict(unit, parsed, 1)
                # After first two are done, set cancel
                if done_count == 2:
                    cancel.set()
                return result
            # Remaining tasks see cancel
            if kwargs.get("cancel") and kwargs["cancel"].is_set():
                return Failure(
                    alias=unit.alias, submission_id=unit.submission_id,
                    path=unit.solution_path, reason=rc.CANCEL_REASON,
                )
            # Shouldn't reach here but just in case
            parsed = rc.validate_payload(
                {"percent": 50, "summary": "late", "remarks": []}
            )
            return rc._make_verdict(unit, parsed, 1)

        with patch.object(rc, "review_unit", new=controlled_review_unit):
            verdicts, failures = await review_all(
                units, api_key="k", base_url="u",
                model_id="m", system_prompt="s", concurrency=2,
                cancel=cancel,
            )

        # The first 2 verdicts should succeed
        self.assertGreaterEqual(len(verdicts), 2)
        # Remaining should be failures with cancel reason
        cancelled = [f for f in failures if rc.CANCEL_REASON in f.reason]
        self.assertGreaterEqual(len(cancelled), 1)

    async def test_review_all_cancel_none_works_normally(self):
        """cancel=None works exactly like before."""
        units = [
            make_unit(self.base, alias="A", sid=1),
            make_unit(self.base, alias="B", sid=2),
        ]
        payload = {"percent": 50, "summary": "s", "remarks": []}

        async def fake_review_unit(unit, *args, **kwargs):
            parsed = rc.validate_payload(payload)
            return rc._make_verdict(unit, parsed, 1)

        with patch.object(rc, "review_unit", new=fake_review_unit):
            verdicts, failures = await review_all(
                units, api_key="k", base_url="u",
                model_id="m", system_prompt="s", concurrency=2,
                cancel=None,
            )
        self.assertEqual(len(verdicts), 2)
        self.assertEqual(len(failures), 0)

    async def test_review_all_cancel_cancels_in_flight(self):
        """When cancel is set, in-flight tasks are cancelled via
        task.cancel() — they don't wait for the full timeout."""
        units = [
            make_unit(self.base, alias="A", sid=i, statement=f"s{i}")
            for i in range(1, 4)
        ]
        cancel = asyncio.Event()

        # Task 1 completes instantly. Task 2 simulates a long-running
        # HTTP request (like a 600s timeout). When cancel is set,
        # task 2 should be cancelled promptly.
        call_count = 0

        async def controlled_review_unit(unit, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Fast task: succeeds immediately
                parsed = rc.validate_payload(
                    {"percent": 90, "summary": "fast", "remarks": []}
                )
                return rc._make_verdict(unit, parsed, 1)
            # Slow task: simulates a long HTTP request
            # CancelledError will be raised by task.cancel()
            await asyncio.sleep(30)
            parsed = rc.validate_payload(
                {"percent": 50, "summary": "late", "remarks": []}
            )
            return rc._make_verdict(unit, parsed, 1)

        async def set_cancel_after_delay():
            await asyncio.sleep(0.1)
            cancel.set()

        with patch.object(rc, "review_unit", new=controlled_review_unit):
            asyncio.create_task(set_cancel_after_delay())
            # Should complete in ~0.1s, not 30s
            verdicts, failures = await asyncio.wait_for(
                review_all(
                    units, api_key="k", base_url="u",
                    model_id="m", system_prompt="s", concurrency=3,
                    cancel=cancel,
                ),
                timeout=5.0,
            )

        # At least 1 verdict (the fast task)
        self.assertGreaterEqual(len(verdicts), 1)
        # The rest should be failures (cancelled)
        cancelled = [f for f in failures if rc.CANCEL_REASON in f.reason]
        self.assertGreaterEqual(len(cancelled), 1)

    async def test_review_all_cancel_is_prompt_with_event_loop(self):
        """cancel.set() scheduled via event loop wakes up the watcher
        immediately — not delayed by in-flight HTTP requests.

        This test validates the fix: using loop.add_signal_handler
        (which calls cancel.set() inside the event loop) instead of
        signal.signal (which calls it from a signal handler, causing
        the event loop to miss the wake-up until current I/O completes).
        """
        units = [
            make_unit(self.base, alias="A", sid=i, statement=f"s{i}")
            for i in range(1, 4)
        ]
        cancel = asyncio.Event()
        call_count = 0

        async def controlled_review_unit(unit, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                parsed = rc.validate_payload(
                    {"percent": 90, "summary": "fast", "remarks": []}
                )
                return rc._make_verdict(unit, parsed, 1)
            await asyncio.sleep(10)
            parsed = rc.validate_payload(
                {"percent": 50, "summary": "late", "remarks": []}
            )
            return rc._make_verdict(unit, parsed, 1)

        # Schedule cancel.set() via loop.call_soon (simulates
        # loop.add_signal_handler calling cancel.set() inside event loop)
        loop = asyncio.get_running_loop()

        def delayed_cancel():
            cancel.set()

        with patch.object(rc, "review_unit", new=controlled_review_unit):
            loop.call_later(0.2, delayed_cancel)
            start = asyncio.get_event_loop().time()
            verdicts, failures = await asyncio.wait_for(
                review_all(
                    units, api_key="k", base_url="u",
                    model_id="m", system_prompt="s", concurrency=3,
                    cancel=cancel,
                ),
                timeout=5.0,
            )
            elapsed = asyncio.get_event_loop().time() - start

        # Must complete quickly (~0.2s), not hang for 10s
        self.assertLess(elapsed, 2.0)
        self.assertGreaterEqual(len(verdicts), 1)
        cancelled = [f for f in failures if rc.CANCEL_REASON in f.reason]
        self.assertGreaterEqual(len(cancelled), 1)


class TestInteractiveReviewKeyboardInterrupt(unittest.TestCase):
    """Ctrl+C during interactive_review is treated as quit."""

    def test_keyboard_interrupt_quits(self):
        v = Verdict(
            unit=SolutionUnit(
                alias="A", submission_id=1, problem_id=None,
                solution_path=Path("/nonexistent/1"),
                statement_path=Path("/nonexistent/s1"),
            ),
            percent=80.0, summary="good", remarks=["none"], attempts=1,
        )
        with patch("builtins.input", side_effect=KeyboardInterrupt), \
             patch("review_contest.read_text_file", return_value="code"):
            ok_list, wa_list, manual, skipped = rc.interactive_review([v])
        self.assertEqual(len(ok_list), 0)
        self.assertEqual(len(wa_list), 0)
        self.assertEqual(len(manual), 0)
        self.assertEqual(len(skipped), 1)


class TestInteractiveReviewAnswers(unittest.TestCase):
    """Test interactive_review answer handling: +, -, Enter, q."""

    def _verdict(self, percent=80.0, alias="A", submission_id=1):
        return Verdict(
            unit=SolutionUnit(
                alias=alias, submission_id=submission_id, problem_id=None,
                solution_path=Path(f"/nonexistent/{submission_id}"),
                statement_path=Path(f"/nonexistent/s{submission_id}"),
            ),
            percent=percent, summary="ok", remarks=[], attempts=1,
        )

    def test_plus_marks_ok(self):
        v = self._verdict()
        with patch("builtins.input", return_value="+"), \
             patch("review_contest.read_text_file", return_value="code"):
            ok_list, wa_list, manual, skipped = rc.interactive_review([v])
        self.assertEqual(ok_list, [v])
        self.assertEqual(wa_list, [])
        self.assertEqual(manual, [])
        self.assertEqual(skipped, [])

    def test_minus_marks_wa(self):
        v = self._verdict()
        with patch("builtins.input", return_value="-"), \
             patch("review_contest.read_text_file", return_value="code"):
            ok_list, wa_list, manual, skipped = rc.interactive_review([v])
        self.assertEqual(ok_list, [])
        self.assertEqual(wa_list, [v])
        self.assertEqual(manual, [])
        self.assertEqual(skipped, [])

    def test_enter_skips_to_manual(self):
        v = self._verdict()
        with patch("builtins.input", return_value=""), \
             patch("review_contest.read_text_file", return_value="code"):
            ok_list, wa_list, manual, skipped = rc.interactive_review([v])
        self.assertEqual(ok_list, [])
        self.assertEqual(wa_list, [])
        self.assertEqual(manual, [v])
        self.assertEqual(skipped, [])

    def test_q_skips_remaining(self):
        v1 = self._verdict(alias="A", submission_id=1)
        v2 = self._verdict(alias="B", submission_id=2)
        with patch("builtins.input", return_value="q"), \
             patch("review_contest.read_text_file", return_value="code"):
            ok_list, wa_list, manual, skipped = rc.interactive_review([v1, v2])
        self.assertEqual(ok_list, [])
        self.assertEqual(wa_list, [])
        self.assertEqual(manual, [])
        self.assertEqual(skipped, [v1, v2])

    def test_eof_quits(self):
        v = self._verdict()
        with patch("builtins.input", side_effect=EOFError), \
             patch("review_contest.read_text_file", return_value="code"):
            ok_list, wa_list, manual, skipped = rc.interactive_review([v])
        self.assertEqual(len(skipped), 1)

    def test_override_model_recommendation(self):
        """Operator can override: model says WA (30%) but operator types + (OK)."""
        v = self._verdict(percent=30.0)
        with patch("builtins.input", return_value="+"), \
             patch("review_contest.read_text_file", return_value="code"):
            ok_list, wa_list, manual, skipped = rc.interactive_review([v])
        self.assertEqual(ok_list, [v])
        self.assertEqual(wa_list, [])

    def test_mixed_answers(self):
        v1 = self._verdict(alias="A", submission_id=1)
        v2 = self._verdict(alias="B", submission_id=2)
        v3 = self._verdict(alias="C", submission_id=3)
        v4 = self._verdict(alias="D", submission_id=4)
        answers = iter(["+", "-", "", "q"])
        with patch("builtins.input", side_effect=lambda _: next(answers)), \
             patch("review_contest.read_text_file", return_value="code"):
            ok_list, wa_list, manual, skipped = rc.interactive_review(
                [v1, v2, v3, v4]
            )
        self.assertEqual(ok_list, [v1])
        self.assertEqual(wa_list, [v2])
        self.assertEqual(manual, [v3])
        self.assertEqual(skipped, [v4])

    def test_source_code_in_prompt(self):
        """Source code is read and displayed in the prompt."""
        v = self._verdict()
        source_text = "print('hello')"
        with patch("builtins.input", return_value="+"), \
             patch("review_contest.read_text_file", return_value=source_text) as mock_read:
            rc.interactive_review([v])
        mock_read.assert_called_with(v.unit.solution_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)