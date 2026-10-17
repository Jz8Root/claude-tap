"""Keep the baseline active-session cache contract during URL recovery fixes."""

import json

import pytest

from claude_tap.dashboard import list_trace_sessions, load_trace_session
from claude_tap.trace_store import SessionQuery, get_trace_store


def _record(turn: int, *, status: int) -> dict:
    body = {
        "content": [{"type": "text", "text": f"Synthetic reply {turn}"}],
        "usage": {"input_tokens": turn * 10, "output_tokens": turn},
    }
    if status == 500:
        body["error"] = "Synthetic middle upstream failure"
    return {
        "turn": turn,
        "timestamp": f"2026-10-14T07:0{turn}:00+00:00",
        "duration_ms": turn * 100,
        "request": {
            "method": "POST",
            "path": "/v1/messages",
            "body": {
                "model": "test-model",
                "messages": [{"role": "user", "content": f"Synthetic prompt {turn}"}],
            },
        },
        "response": {"status": status, "body": body},
    }


def _raw_payloads(store, session_id: str) -> list[str]:
    return [
        row["payload_json"]
        for row in store._connect().execute(
            "SELECT payload_json FROM records WHERE session_id = ? ORDER BY record_index", (session_id,)
        )
    ]


@pytest.mark.parametrize("reader", ["session-list", "session-detail"])
@pytest.mark.parametrize("middle_error", [False, True], ids=["all-success", "middle-error"])
def test_active_v4_cache_reads_preserve_baseline_history(trace_db, reader: str, middle_error: bool) -> None:
    store = get_trace_store()
    session_id = store.create_session(client="claude", proxy_mode="reverse")
    records = [_record(turn, status=500 if middle_error and turn == 2 else 200) for turn in range(1, 4)]
    for record in records:
        store.append_record(session_id, record)

    cached = json.loads(store.load_session_row(session_id)["summary_json"])
    expected_error = "Synthetic middle upstream failure" if middle_error else ""
    assert cached["status"] == ("error" if middle_error else "active")
    assert cached["error"] == expected_error
    # The captured cache comes from version 4, regardless of the reader version.
    cached["summary_version"] = 4
    conn = store._connect()
    conn.execute(
        "UPDATE sessions SET summary_json = ? WHERE id = ?",
        (json.dumps(cached, ensure_ascii=False, separators=(",", ":")), session_id),
    )
    conn.commit()
    row_before = dict(store.load_session_row(session_id))
    payloads_before = _raw_payloads(store, session_id)
    aggregates_before = store.get_session_aggregates()
    assert aggregates_before["total_errors"] == int(middle_error)
    assert aggregates_before["total_records"] == 3
    assert aggregates_before["total_tokens"] == 66
    assert store.count_session_rows(SessionQuery(status="error")) == int(middle_error)
    assert store.count_session_rows(SessionQuery(status="active")) == int(not middle_error)

    if reader == "session-list":
        summaries = list_trace_sessions()
        assert len(summaries) == 1
        summary = summaries[0]
    else:
        detail = load_trace_session(session_id)
        assert detail is not None
        summary = detail["session"]

    # Baseline readers normalize the returned lifecycle state without rewriting
    # the persisted error marker used by history filters and aggregates.
    assert summary["id"] == session_id
    assert summary["status"] == "active"
    assert summary["active"] is True
    assert summary["error"] == expected_error
    assert summary["updated_at"] == row_before["updated_at"]
    assert summary["record_count"] == 3
    assert summary["input_tokens"] == 60
    assert summary["output_tokens"] == 6
    assert summary["total_tokens"] == 66
    assert summary["first_user"] == "Synthetic prompt 1"
    assert summary["last_response"] == "Synthetic reply 3"
    assert store.get_session_aggregates() == aggregates_before
    assert store.count_session_rows(SessionQuery(status="error")) == int(middle_error)
    assert store.count_session_rows(SessionQuery(status="active")) == int(not middle_error)
    assert _raw_payloads(store, session_id) == payloads_before
    assert store.load_records(session_id) == records
    assert dict(store.load_session_row(session_id)) == row_before
