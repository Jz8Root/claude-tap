"""Focused tests for forward-proxy request capture."""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from multidict import CIMultiDict

from claude_tap.cli import BROWSER_TRACE_METHODS, BROWSER_TRACE_PATH_PREFIXES
from claude_tap.cli_clients import CLIENT_CONFIGS
from claude_tap.forward_proxy import (
    ForwardProxyServer,
    _decode_raw_stream_body,
    _decode_request_body_for_trace,
    _is_streaming_response,
    _is_unbounded_response,
    _matches_path_prefix,
)
from claude_tap.trace import TraceWriter
from claude_tap.trace_store import get_trace_store, reset_trace_store

try:
    from compression import zstd
except ImportError:
    import backports.zstd as zstd


class _UnexpectedSession:
    async def request(self, **_kwargs: Any) -> None:
        raise AssertionError("capture-only mode must not contact upstream")


class _MemoryWriter:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/backend-api/codex/responses", True),
        ("POST", "/v1/responses", True),
        ("WEBSOCKET", "/v1/responses", True),
        ("GET", "/v1/responses", False),
        ("POST", "/v1/chat/completions", False),
        ("POST", "/v1/responses-other", False),
    ],
)
def test_codexapp_capture_filter(method: str, path: str, expected: bool) -> None:
    cfg = CLIENT_CONFIGS["codexapp"]
    server = ForwardProxyServer(
        host="127.0.0.1",
        port=0,
        ca=object(),
        writer=object(),
        session=object(),
        trace_methods=cfg.forward_trace_methods,
        trace_path_prefixes=cfg.forward_trace_path_prefixes,
    )

    assert server._should_trace_request(method, path) is expected


@pytest.mark.asyncio
async def test_forward_proxy_captures_codexapp_custom_responses_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDTAP_DB", str(tmp_path / "traces.sqlite3"))
    reset_trace_store()
    store = get_trace_store()
    session_id = store.create_session(client="codexapp", proxy_mode="forward")
    trace_writer = TraceWriter(session_id, store=store)
    client_writer = _MemoryWriter()
    cfg = CLIENT_CONFIGS["codexapp"]
    server = ForwardProxyServer(
        host="127.0.0.1",
        port=0,
        ca=object(),
        writer=trace_writer,
        session=_UnexpectedSession(),
        trace_methods=cfg.forward_trace_methods,
        trace_path_prefixes=cfg.forward_trace_path_prefixes,
        capture_only=True,
    )
    request_body = {
        "model": "MiniMax-M3",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "stream": False,
    }

    try:
        await server._forward_and_record(
            "POST",
            "/v1/responses",
            {"Content-Type": "application/json"},
            json.dumps(request_body).encode(),
            "https://api.minimaxi.com/v1/responses",
            client_writer,
        )
        trace_writer.close()

        records = store.load_records(session_id)
        assert len(records) == 1
        assert records[0]["request"]["path"] == "/v1/responses"
        assert records[0]["request"]["body"] == request_body
        assert records[0]["response"]["status"] == 200
    finally:
        reset_trace_store()


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
        ("zstd", zstd.compress),
    ],
)
def test_decode_request_body_for_trace_supports_common_encodings(
    encoding: str,
    compress: Callable[[bytes], bytes],
) -> None:
    payload = b'{"model":"gpt-5.6-luna","stream":true}'
    assert _decode_request_body_for_trace(compress(payload), {"Content-Encoding": encoding}) == payload


def test_decode_request_body_for_trace_leaves_unknown_or_broken_bodies() -> None:
    payload = b'{"model":"gpt-5.6-luna"}'
    assert _decode_request_body_for_trace(payload, {}) == payload
    assert _decode_request_body_for_trace(payload, {"Content-Encoding": "identity"}) == payload
    assert _decode_request_body_for_trace(payload, {"Content-Encoding": "br"}) == payload
    assert _decode_request_body_for_trace(b"not-zstd", {"Content-Encoding": "zstd"}) == b"not-zstd"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding", "compress"),
    [
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
        ("zstd", zstd.compress),
    ],
)
async def test_forward_proxy_captures_compressed_pi_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
    compress: Callable[[bytes], bytes],
) -> None:
    monkeypatch.setenv("CLOUDTAP_DB", str(tmp_path / "traces.sqlite3"))
    reset_trace_store()
    store = get_trace_store()
    session_id = store.create_session(client="pi", proxy_mode="forward")
    trace_writer = TraceWriter(session_id, store=store)
    client_writer = _MemoryWriter()
    server = ForwardProxyServer(
        host="127.0.0.1",
        port=0,
        ca=object(),
        writer=trace_writer,
        session=_UnexpectedSession(),
        store_stream_events=True,
        capture_only=True,
    )
    request_body = {
        "model": "gpt-5.6-luna",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "stream": True,
    }
    compressed_body = compress(json.dumps(request_body).encode())

    try:
        await server._forward_and_record(
            "POST",
            "/backend-api/codex/responses",
            {"Content-Type": "application/json", "Content-Encoding": encoding},
            compressed_body,
            "https://chatgpt.com/backend-api/codex/responses",
            client_writer,
        )
        trace_writer.close()

        records = store.load_records(session_id)
        assert len(records) == 1
        assert records[0]["request"]["body"] == request_body
        assert records[0]["response"]["body"]["output"][0]["content"][0]["text"] == "captured"
        assert b"Content-Type: text/event-stream" in client_writer.data
    finally:
        reset_trace_store()


class _FakeUpstreamResponse:
    """aiohttp.ClientResponse stand-in that fails if the proxy consumes the body the wrong way."""

    status = 200
    reason = "OK"

    def __init__(
        self,
        headers: dict[str, str],
        chunks: list[bytes],
        client_writer: _MemoryWriter,
        *,
        allow_read: bool,
    ) -> None:
        self.headers = CIMultiDict(headers)
        self.content = self
        self._chunks = chunks
        self._client_writer = client_writer
        self._allow_read = allow_read

    async def iter_any(self):
        if self._allow_read:
            raise AssertionError("buffered responses must be read, not iterated")
        for index, chunk in enumerate(self._chunks):
            if index:
                assert self._chunks[index - 1] in self._client_writer.data, "previous chunk was not relayed yet"
            yield chunk

    async def read(self) -> bytes:
        if not self._allow_read:
            raise AssertionError("streamed responses must not be buffered")
        return b"".join(self._chunks)

    def close(self) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeUpstreamResponse) -> None:
        self._response = response

    async def request(self, **_kwargs: Any) -> _FakeUpstreamResponse:
        return self._response


@pytest.fixture
def trace_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, str, TraceWriter]]:
    monkeypatch.setenv("CLOUDTAP_DB", str(tmp_path / "traces.sqlite3"))
    reset_trace_store()
    store = get_trace_store()
    session_id = store.create_session(client="browser", proxy_mode="forward")
    yield store, session_id, TraceWriter(session_id, store=store)
    reset_trace_store()


def _connect_frame(payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, separators=(",", ":")).encode()
    return b"\x00" + len(data).to_bytes(4, "big") + data


async def _forward_through_fake_upstream(
    trace_writer: TraceWriter,
    client_writer: _MemoryWriter,
    response: _FakeUpstreamResponse,
    upstream_url: str,
    *,
    request_headers: dict[str, str] | None = None,
    request_body: bytes = b"{}",
) -> None:
    path = "/" + upstream_url.split("/", 3)[3]
    server = ForwardProxyServer(
        host="127.0.0.1",
        port=0,
        ca=object(),
        writer=trace_writer,
        session=_FakeSession(response),
        trace_methods=("POST",),
        trace_path_prefixes=("/claudeai-rpc/", "/api/"),
    )
    await server._forward_and_record(
        "POST",
        path,
        request_headers or {"Content-Type": "application/connect+json"},
        request_body,
        upstream_url,
        client_writer,
    )
    trace_writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    ["application/connect+json", "application/grpc-web+proto", "application/x-ndjson"],
)
async def test_forward_proxy_relays_non_sse_stream_incrementally(
    trace_env: tuple[Any, str, TraceWriter],
    content_type: str,
) -> None:
    store, session_id, trace_writer = trace_env
    client_writer = _MemoryWriter()
    chunks = [_connect_frame({"text": "hel"}), _connect_frame({"text": "lo"})]
    response = _FakeUpstreamResponse(
        {"Content-Type": content_type, "Content-Length": "999", "Content-Encoding": "gzip"},
        chunks,
        client_writer,
        allow_read=False,
    )

    await _forward_through_fake_upstream(
        trace_writer, client_writer, response, "https://claude.ai/claudeai-rpc/svc.Service/StreamTimeline"
    )

    sent = bytes(client_writer.data)
    head = sent.split(b"\r\n\r\n", 1)[0].lower()
    assert b"transfer-encoding: chunked" in head
    assert b"content-length" not in head
    assert b"content-encoding" not in head
    assert all(chunk in sent for chunk in chunks)

    [record] = store.load_records(session_id)
    body = record["response"]["body"]
    assert isinstance(body, str)
    assert '"text":"hel"' in body
    assert '"text":"lo"' in body


@pytest.mark.asyncio
async def test_forward_proxy_keeps_binary_stream_body_losslessly(trace_env: tuple[Any, str, TraceWriter]) -> None:
    store, session_id, trace_writer = trace_env
    client_writer = _MemoryWriter()
    chunks = [b"\x08\x01\xff\xfe", b"\x12\x03abc\x80"]
    response = _FakeUpstreamResponse(
        {"Content-Type": "application/connect+proto"}, chunks, client_writer, allow_read=False
    )

    await _forward_through_fake_upstream(
        trace_writer, client_writer, response, "https://claude.ai/claudeai-rpc/svc.Service/StreamTimeline"
    )

    [record] = store.load_records(session_id)
    body = record["response"]["body"]
    assert body["_encoding"] == "binary"
    assert body["byte_length"] == 10
    assert base64.b64decode(body["base64"]) == b"".join(chunks)


@pytest.mark.asyncio
async def test_forward_proxy_streams_unbounded_response_on_web_ai_host(
    trace_env: tuple[Any, str, TraceWriter],
) -> None:
    store, session_id, trace_writer = trace_env
    client_writer = _MemoryWriter()
    response = _FakeUpstreamResponse(
        {"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
        [b'{"ok":', b"true}"],
        client_writer,
        allow_read=False,
    )

    await _forward_through_fake_upstream(
        trace_writer, client_writer, response, "https://claude.ai/api/organizations/org/stream"
    )

    [record] = store.load_records(session_id)
    assert record["response"]["body"] == {"ok": True}


@pytest.mark.asyncio
async def test_forward_proxy_still_buffers_unbounded_response_on_other_hosts(
    trace_env: tuple[Any, str, TraceWriter],
) -> None:
    store, session_id, trace_writer = trace_env
    client_writer = _MemoryWriter()
    response = _FakeUpstreamResponse(
        {"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
        [b'{"ok":', b"true}"],
        client_writer,
        allow_read=True,
    )

    await _forward_through_fake_upstream(trace_writer, client_writer, response, "https://api.example.com/api/items")

    head = bytes(client_writer.data).split(b"\r\n\r\n", 1)[0].lower()
    assert b"content-length: 11" in head
    assert b"transfer-encoding" not in head
    [record] = store.load_records(session_id)
    assert record["response"]["body"] == {"ok": True}


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Content-Type": "text/event-stream; charset=utf-8"}, True),
        ({"content-type": "application/connect+json"}, True),
        ({"Content-Type": "application/grpc-web-text"}, True),
        ({"Content-Type": "application/x-ndjson"}, True),
        ({"Content-Type": "application/json"}, False),
        ({}, False),
    ],
)
def test_is_streaming_response(headers: dict[str, str], expected: bool) -> None:
    assert _is_streaming_response(headers) is expected


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Transfer-Encoding": "chunked"}, True),
        ({"transfer-encoding": "Chunked"}, True),
        ({"Transfer-Encoding": "chunked", "Content-Length": "12"}, False),
        ({"Content-Length": "12"}, False),
        ({}, False),
    ],
)
def test_is_unbounded_response(headers: dict[str, str], expected: bool) -> None:
    assert _is_unbounded_response(headers) is expected


def test_decode_raw_stream_body() -> None:
    framed = _connect_frame({"a": 1})
    binary = b"\x08\x01\xff"

    assert _decode_raw_stream_body(b"") is None
    assert _decode_raw_stream_body(b'{"a": 1}') == {"a": 1}
    assert _decode_raw_stream_body(b"plain text") == "plain text"
    assert _decode_raw_stream_body(framed) == framed.decode()
    blob = _decode_raw_stream_body(binary)
    assert isinstance(blob, dict)
    assert base64.b64decode(blob["base64"]) == binary


_CLAUDEAI_RPC = "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService"


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/organizations/org/chat_conversations/conv/completion", True),
        ("POST", f"{_CLAUDEAI_RPC}/PerformAction", True),
        ("POST", f"{_CLAUDEAI_RPC}/StreamTimeline?cursor=1", True),
        ("POST", f"{_CLAUDEAI_RPC}/ReportViewing", False),
        ("POST", "/api/event_logging/v2/batch", False),
        ("POST", "/backend-api/conversation", True),
        ("GET", f"{_CLAUDEAI_RPC}/StreamTimeline", False),
        ("GET", "/api/organizations/org/members", False),
    ],
)
def test_browser_trace_filter(method: str, path: str, expected: bool) -> None:
    server = ForwardProxyServer(
        host="127.0.0.1",
        port=0,
        ca=object(),
        writer=object(),
        session=object(),
        trace_methods=BROWSER_TRACE_METHODS,
        trace_path_prefixes=BROWSER_TRACE_PATH_PREFIXES,
    )

    assert server._should_trace_request(method, path) is expected


@pytest.mark.parametrize(
    ("path", "prefixes", "expected"),
    [
        ("/api/organizations/abc/x", ("/api/organizations/",), True),
        ("/api/organizations/abc/x", ("/api/organizations",), True),
        ("/api/organizations", ("/api/organizations/",), True),
        ("/api/organizations-other/x", ("/api/organizations/",), False),
        ("/v1/messages?beta=1", ("/v1/messages",), True),
        ("/v1/messages-other", ("/v1/messages",), False),
    ],
)
def test_matches_path_prefix_ignores_trailing_slash_in_prefix(
    path: str, prefixes: tuple[str, ...], expected: bool
) -> None:
    assert _matches_path_prefix(path, prefixes) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_url", "keeps_bytes"),
    [
        ("https://claude.ai/claudeai-rpc/svc.Service/PerformAction", True),
        ("https://api.example.com/api/svc.Service/PerformAction", False),
    ],
)
async def test_forward_proxy_keeps_protobuf_request_bytes_only_for_web_ai_hosts(
    trace_env: tuple[Any, str, TraceWriter],
    upstream_url: str,
    keeps_bytes: bool,
) -> None:
    store, session_id, trace_writer = trace_env
    client_writer = _MemoryWriter()
    request_body = b"\x0a\x05hello\xff"
    response = _FakeUpstreamResponse(
        {"Content-Type": "application/proto", "Content-Length": "0"}, [], client_writer, allow_read=True
    )

    await _forward_through_fake_upstream(
        trace_writer,
        client_writer,
        response,
        upstream_url,
        request_headers={"Content-Type": "application/connect+proto"},
        request_body=request_body,
    )

    [record] = store.load_records(session_id)
    body = record["request"]["body"]
    assert body["_encoding"] == "protobuf"
    assert body["byte_length"] == len(request_body)
    if keeps_bytes:
        assert base64.b64decode(body["base64"]) == request_body
    else:
        assert "base64" not in body


@pytest.mark.asyncio
async def test_forward_proxy_streams_real_connect_response_live(trace_env: tuple[Any, str, TraceWriter]) -> None:
    store, session_id, trace_writer = trace_env
    client_writer = _MemoryWriter()
    frames = [_connect_frame({"text": "hel"}), _connect_frame({"text": "lo"})]
    first_frame_relayed = asyncio.Event()
    path = "/claudeai-rpc/svc.Service/StreamTimeline"

    async def upstream_handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "application/connect+json"})
        await response.prepare(request)
        await response.write(frames[0])
        # Only continue once the proxy has handed the first frame to the client.
        await asyncio.wait_for(first_frame_relayed.wait(), timeout=5)
        await response.write(frames[1])
        await response.write_eof()
        return response

    async def watch_client() -> None:
        while frames[0] not in client_writer.data:
            await asyncio.sleep(0.01)
        first_frame_relayed.set()

    app = web.Application()
    app.router.add_post(path, upstream_handler)
    async with TestServer(app) as upstream, aiohttp.ClientSession() as session:
        server = ForwardProxyServer(
            host="127.0.0.1",
            port=0,
            ca=object(),
            writer=trace_writer,
            session=session,
            trace_methods=("POST",),
            trace_path_prefixes=("/claudeai-rpc/",),
        )
        watcher = asyncio.create_task(watch_client())
        try:
            await asyncio.wait_for(
                server._forward_and_record(
                    "POST",
                    path,
                    {"Content-Type": "application/connect+json"},
                    b"{}",
                    str(upstream.make_url(path)),
                    client_writer,
                ),
                timeout=10,
            )
        finally:
            watcher.cancel()
    trace_writer.close()

    assert first_frame_relayed.is_set()
    assert all(frame in bytes(client_writer.data) for frame in frames)
    [record] = store.load_records(session_id)
    assert '"text":"hel"' in record["response"]["body"]
    assert '"text":"lo"' in record["response"]["body"]
