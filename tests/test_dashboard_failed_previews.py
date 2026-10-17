"""Keep failed URL fallback inside one display response and out of capture."""

import asyncio
from copy import deepcopy
from json import JSONDecoder, dumps, loads
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import make_mocked_request

from claude_tap import dashboard
from claude_tap.compact_trace import load_compact_trace, materialize_compact_trace_bundle
from claude_tap.live import LiveViewerServer
from claude_tap.trace_store import get_trace_store

SECRET = "display-userinfo-secret"
BAD = "https://user:" + SECRET + "@host：443/path?token=query-secret"
LONG_BAD = "https://user:" + SECRET + "@host：443/" + "x" * 260 + "?token=query-secret"
OLD_PREFIX = LONG_BAD[:219] + "..."
SUMMARY_FIELDS = ("first_user", "last_response", "error")


def record(user: str = "public user", *, system: str = "public system", error: str = "", turn: int = 1) -> dict:
    body = {"content": [{"type": "text", "text": "public response"}]}
    if error:
        body["error"] = {"message": error}
    return {
        "timestamp": "2026-10-15T16:00:00+00:00",
        "request_id": "display-" + str(turn),
        "turn": turn,
        "request": {
            "method": "POST",
            "path": "/v1/messages",
            "body": {"model": "public-model", "system": system, "messages": [{"role": "user", "content": user}]},
        },
        "response": {"status": 500 if error else 200, "body": body},
    }


def cached_summary(*, version: int = 4, status: str = "complete") -> dict:
    return {
        "summary_version": version,
        "status": status,
        "agent": "Claude Code",
        "model": "cached-model",
        "first_user": OLD_PREFIX,
        "last_response": "cached public response",
        "error": "cached public error",
        "turn_count": 6,
        "duration_ms": 987,
        "input_tokens": 31,
        "output_tokens": 11,
        "cache_read_tokens": 7,
        "cache_read_in_input_tokens": 0,
        "cache_create_tokens": 5,
        "total_tokens": 54,
    }


def seed(
    store, records: list[dict], *, cached: dict | None, status: str = "complete", legacy_path: str | None = None
) -> str:
    """Seed preexisting capture independently of capture-time summary refresh."""
    session_id = store.create_session(client="claude", proxy_mode="reverse")
    connection = store._connect()
    for index, item in enumerate(records, 1):
        connection.execute(
            "INSERT INTO records(session_id,record_index,turn,timestamp,payload_json) VALUES(?,?,?,?,?)",
            (session_id, index, item["turn"], item["timestamp"], dumps(item, ensure_ascii=False)),
        )
    summary = {**cached, "id": session_id, "record_count": len(records)} if cached is not None else None
    connection.execute(
        "UPDATE sessions SET status=?,record_count=?,summary_json=?,legacy_rel_path=? WHERE id=?",
        (
            status,
            len(records),
            dumps(summary, ensure_ascii=False) if summary is not None else None,
            legacy_path,
            session_id,
        ),
    )
    connection.commit()
    return session_id


def snapshot(store, session_id: str) -> tuple:
    return (
        dict(store.load_session_row(session_id)),
        [
            tuple(row)
            for row in store._connect().execute(
                "SELECT * FROM records WHERE session_id=? ORDER BY record_index", (session_id,)
            )
        ],
        store.export_jsonl(session_id),
        store.export_compact(session_id),
    )


def assert_hidden(summary: dict) -> None:
    for field in SUMMARY_FIELDS:
        assert summary[field] == "REDACTED"


def request(session_id: str, suffix: str):
    return make_mocked_request("GET", "/api/sessions/" + session_id + suffix, match_info={"session_id": session_id})


@pytest.mark.parametrize(("version", "field"), [(4, "first_user"), (4, "error"), (3, "error"), (3, "model")])
def test_cached_failure_hides_response_previews_without_persisting_migration(
    trace_db, monkeypatch, version: int, field: str
) -> None:
    store = get_trace_store()
    cached = cached_summary(version=version)
    cached[field] = BAD
    session_id = seed(store, [record()], cached=cached)
    before = snapshot(store, session_id)

    def forbidden_store(*args, **kwargs):
        pytest.fail("A summary produced after a URL failure must not be persisted")

    monkeypatch.setattr(store, "store_summary", forbidden_store)
    original_load = store.load_records
    if version == 4:

        def forbidden_scan(*args, **kwargs):
            pytest.fail("A current cached summary must not scan capture to repair previews")

        monkeypatch.setattr(store, "load_records", forbidden_scan)
    summary = dashboard.list_trace_sessions()[0]
    assert_hidden(summary)
    assert summary["status"] == "complete"
    assert summary["duration_ms"] == 987
    assert summary["turn_count"] == 6
    assert summary["total_tokens"] == 54
    monkeypatch.setattr(store, "load_records", original_load)
    assert snapshot(store, session_id) == before


def summary_branch(store, branch: str, failed: bool) -> tuple[str, bool]:
    if branch == "migration":
        cached = cached_summary(version=3)
        cached["model"] = BAD if failed else "cached-model"
        return seed(store, [record()], cached=cached), False
    if branch == "empty":
        return seed(store, [], cached=None, legacy_path=BAD if failed else "legacy/trace.jsonl"), False
    if branch == "cold_error":
        return seed(store, [record(error=BAD if failed else "public error")], cached=None, status="error"), False
    return seed(store, [record(error=BAD if failed else "public error")], cached=None), True


@pytest.mark.parametrize("branch", ["migration", "empty", "cold_error", "full_scan"])
@pytest.mark.parametrize("failed", [False, True])
def test_each_existing_cache_write_is_retained_only_when_that_load_observes_no_failure(
    trace_db, monkeypatch, branch: str, failed: bool
) -> None:
    store = get_trace_store()
    session_id, allow_scan = summary_branch(store, branch, failed)
    before = snapshot(store, session_id)
    real_store = store.store_summary
    writes = []

    def observe_write(*args, **kwargs):
        writes.append(deepcopy(args[1]))
        return real_store(*args, **kwargs)

    monkeypatch.setattr(store, "store_summary", observe_write)
    with dashboard.dashboard_redaction_scope() as failures:
        summary = dashboard._session_summary_from_row(
            store, store.load_session_row(session_id), allow_record_scan=allow_scan
        )
        assert bool(failures) is failed
    assert len(writes) == (0 if failed else 1)
    if failed:
        assert snapshot(store, session_id) == before
        assert "REDACTED" in dumps(summary)
    else:
        assert loads(store.load_session_row(session_id)["summary_json"])["summary_version"] == 4
        assert snapshot(store, session_id)[1:] == before[1:]


def test_failure_before_summary_entry_does_not_disable_an_otherwise_normal_cache_write(trace_db, monkeypatch) -> None:
    store = get_trace_store()
    session_id = seed(store, [], cached=None)
    real_store = store.store_summary
    writes = []

    def observe_write(*args, **kwargs):
        writes.append(args[0])
        return real_store(*args, **kwargs)

    monkeypatch.setattr(store, "store_summary", observe_write)
    with dashboard.dashboard_redaction_scope() as failures:
        assert dashboard._redact_sensitive_text(BAD) == "REDACTED"
        count_before = len(failures)
        dashboard._session_summary_from_row(store, store.load_session_row(session_id))
        assert len(failures) == count_before
    assert writes == [session_id]


def test_list_scopes_are_independent_for_each_cached_row(trace_db) -> None:
    store = get_trace_store()
    bad = cached_summary()
    bad["first_user"] = BAD
    bad_id = seed(store, [record()], cached=bad)
    good = cached_summary()
    good["first_user"] = "public cached user"
    good_id = seed(store, [record(turn=2)], cached=good)
    summaries = {item["id"]: item for item in dashboard.list_trace_sessions()}
    assert_hidden(summaries[bad_id])
    assert summaries[good_id]["first_user"] == "public cached user"
    assert summaries[good_id]["last_response"] == "cached public response"
    assert summaries[good_id]["error"] == "cached public error"


@pytest.mark.asyncio
async def test_paginated_requests_mask_only_when_the_requested_page_observes_failure(trace_db, monkeypatch) -> None:
    store = get_trace_store()
    records = [record(turn=1), record(LONG_BAD, system=LONG_BAD, turn=2), record(turn=3)]
    session_id = seed(store, records, cached=cached_summary())
    before = snapshot(store, session_id)
    original_load = store.load_records
    reads = []

    def observe_read(*args, **kwargs):
        reads.append((kwargs.get("offset"), kwargs.get("limit")))
        return original_load(*args, **kwargs)

    monkeypatch.setattr(store, "load_records", observe_read)
    server = LiveViewerServer()
    for offset in (0, 1, 0):
        response = await server._handle_session_records(request(session_id, f"/records?offset={offset}&limit=1"))
        assert response.status == 200
        payload = loads(response.text)
        assert len(payload["records"]) == 1
        if offset == 1:
            assert_hidden(payload["session"])
            assert payload["records"][0]["request"]["body"]["system"] == "REDACTED"
        else:
            # No full scan is added: this old prefix was already visible when
            # the original requested page did not visit the failed source.
            assert payload["session"]["first_user"] == OLD_PREFIX
            assert payload["session"]["last_response"] == "cached public response"
            assert payload["records"][0]["request"]["body"]["messages"][0]["content"] == "public user"
    assert reads == [(0, 1), (1, 1), (0, 1)]
    assert snapshot(store, session_id) == before


@pytest.mark.asyncio
async def test_cross_record_html_preview_mask_keeps_public_metadata_and_raw_exports(trace_db) -> None:
    store = get_trace_store()
    records = [record(system=LONG_BAD, error="first public error", turn=1), record(BAD, error="public error", turn=2)]
    session_id = seed(store, records, cached=cached_summary())
    before = snapshot(store, session_id)
    server = LiveViewerServer()
    response = await server._handle_session_records(request(session_id, "/records"))
    payload = loads(response.text)
    assert response.status == 200
    assert_hidden(payload["session"])
    assert payload["records"][0]["request"]["body"]["messages"][0]["content"] == "public user"
    assert payload["records"][1]["request"]["body"]["messages"][0]["content"] == "REDACTED"
    assert payload["records"][1]["response"]["body"]["error"]["message"] == "public error"
    response = await server._session_html_response(session_id)
    assert response.status == 200
    metadata, _ = JSONDecoder().raw_decode(response.text.partition("const EMBEDDED_TRACE_META = ")[2])
    for index, item in enumerate(metadata):
        for field in ("sys_hint", "session_user_text", "error_message"):
            assert item[field] == "REDACTED"
        assert item["request_id"] == records[index]["request_id"]
        assert item["method"] == "POST"
        assert item["path"] == "/v1/messages"
    assert SECRET not in response.text
    jsonl = await server._handle_export_jsonl(request(session_id, "/export/jsonl"))
    assert [loads(line) for line in jsonl.text.splitlines()] == records
    compact = await server._handle_export_compact(request(session_id, "/export/compact"))
    assert load_compact_trace(compact.text) == records
    download = await server._handle_export_html(request(session_id, "/export/html"))
    bundle, _ = JSONDecoder().raw_decode(download.text.partition("const EMBEDDED_TRACE_COMPACT_DATA = ")[2])
    assert materialize_compact_trace_bundle(bundle) == records
    assert snapshot(store, session_id) == before


@pytest.mark.parametrize("container", ["json", "form", "uri"])
def test_nested_failed_leaf_returns_to_the_original_outer_container_inside_display_scope(container: str) -> None:
    if container == "json":
        value = dumps({"payload": BAD, "public": ["keep", 3]}, ensure_ascii=False, separators=(",", ":"))
        expected = '{"payload":"REDACTED","public":["keep",3]}'
    else:
        prefix = "https://outer.example/path?" if container == "uri" else ""
        value = prefix + urlencode({"payload": BAD, "public": "keep"})
        expected = prefix + "payload=REDACTED&public=keep"
    with dashboard.dashboard_redaction_scope() as failures:
        assert dashboard._redact_sensitive_text(value) == expected
        assert failures
    with pytest.raises(ValueError):
        dashboard._redact_sensitive_text(value)


def test_scope_finally_restores_the_original_outside_display_exception() -> None:
    with pytest.raises(RuntimeError, match="caller stopped"):
        with dashboard.dashboard_redaction_scope() as failures:
            assert dashboard._redact_sensitive_text(BAD) == "REDACTED"
            assert failures
            raise RuntimeError("caller stopped")
    with pytest.raises(ValueError):
        dashboard._redact_sensitive_text(BAD)


def test_nested_scope_restores_parent_failure_state() -> None:
    with dashboard.dashboard_redaction_scope() as outer:
        assert dashboard._redact_sensitive_text(BAD) == "REDACTED"
        before = len(outer)
        with dashboard.dashboard_redaction_scope() as inner:
            assert dashboard._redact_sensitive_text("token=normal-secret") == "token=REDACTED"
            assert not inner
        assert len(outer) == before
        assert dashboard._redact_sensitive_text(BAD) == "REDACTED"
        assert len(outer) == before + 1


@pytest.mark.asyncio
async def test_concurrent_display_scopes_do_not_share_failure_flags() -> None:
    failed_ready = asyncio.Event()
    release_failed = asyncio.Event()

    async def failed_request():
        with dashboard.dashboard_redaction_scope() as failures:
            assert dashboard._redact_sensitive_text(BAD) == "REDACTED"
            failed_ready.set()
            await release_failed.wait()
            return list(failures)

    async def successful_request():
        await failed_ready.wait()
        try:
            with dashboard.dashboard_redaction_scope() as failures:
                await asyncio.sleep(0)
                assert dashboard._redact_sensitive_text("token=normal-secret") == "token=REDACTED"
                return list(failures)
        finally:
            release_failed.set()

    failed, successful = await asyncio.gather(failed_request(), successful_request())
    assert failed
    assert successful == []
    with pytest.raises(ValueError):
        dashboard._redact_sensitive_text(BAD)


def test_capture_append_and_import_keep_their_original_outside_display_behavior(trace_db) -> None:
    store = get_trace_store()
    session_id = store.create_session(client="claude", proxy_mode="reverse")
    store.append_record(session_id, record(LONG_BAD))
    assert loads(store.load_session_row(session_id)["summary_json"])["first_user"] == OLD_PREFIX
    row = store.load_session_row(session_id)
    with pytest.raises(ValueError):
        dashboard.build_imported_session_summary(row, [record(BAD)], {})
    failed_id = store.create_session(client="claude", proxy_mode="reverse")
    try:
        with pytest.raises(ValueError):
            store.append_record(failed_id, record(BAD))
    finally:
        store._connect().rollback()
