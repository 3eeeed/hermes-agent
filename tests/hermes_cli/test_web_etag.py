"""Conditional GET and chunk deltas for repeated Desktop JSON polls (ETag / 304 / delta)."""

import hashlib
import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from hermes_cli.web_etag import JsonETagMiddleware, apply_delta, build_delta


def _client(state):
    app = FastAPI()
    app.add_middleware(JsonETagMiddleware)

    @app.get("/api/data")
    def data():
        return {"messages": state["messages"]}

    @app.get("/api/stream")
    def stream():
        return StreamingResponse(iter([b"data: 1\n\n", b"data: 2\n\n"]), media_type="text/event-stream")

    return TestClient(app)


def _rows(n, changed=None):
    return [{"id": f"s{i}", "title": f"session {i}", "last_active": 1000 + (99 if i == changed else 0),
             "preview": "x" * 400} for i in range(n)]


def test_unchanged_body_revalidates_to_empty_304_and_changed_body_gets_new_tag():
    state = {"messages": ["a" * 5000]}
    client = _client(state)

    first = client.get("/api/data")
    etag = first.headers["etag"]
    assert first.status_code == 200 and first.json() == {"messages": state["messages"]}

    again = client.get("/api/data", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag

    state["messages"].append("b")
    changed = client.get("/api/data", headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag
    assert changed.json()["messages"][-1] == "b"
    assert int(changed.headers["content-length"]) == len(changed.content)


def test_one_changed_row_ships_as_a_small_delta_that_rebuilds_the_exact_body():
    state = {"messages": _rows(80)}
    client = _client(state)
    base_res = client.get("/api/data")
    base, etag = base_res.content, base_res.headers["etag"]

    state["messages"] = _rows(80, changed=5)
    delta = client.get("/api/data", headers={"If-None-Match": etag, "X-Hermes-Delta": "1"})
    full = client.get("/api/data")

    assert delta.headers["x-hermes-delta-base"] == etag
    assert delta.headers["etag"] == full.headers["etag"]
    assert len(delta.content) < len(full.content) / 10
    rebuilt = apply_delta(base, json.loads(delta.content))
    assert rebuilt == full.content
    assert 'W/"' + hashlib.sha256(rebuilt).hexdigest()[:32] + '"' == full.headers["etag"]


def test_client_without_delta_opt_in_or_unknown_base_gets_the_full_body():
    state = {"messages": _rows(80)}
    client = _client(state)
    etag = client.get("/api/data").headers["etag"]
    state["messages"] = _rows(80, changed=1)

    plain = client.get("/api/data", headers={"If-None-Match": etag})
    unknown = client.get("/api/data", headers={"If-None-Match": 'W/"nope"', "X-Hermes-Delta": "1"})

    for res in (plain, unknown):
        assert "x-hermes-delta-base" not in res.headers
        assert res.json() == {"messages": state["messages"]}


def test_delta_roundtrip_survives_reordered_inserted_and_removed_rows():
    base = json.dumps({"rows": _rows(30)}).encode()
    rows = _rows(30, changed=3)
    rows.insert(0, {"id": "new", "title": "fresh"})
    del rows[10]
    rows[4], rows[20] = rows[20], rows[4]
    new = json.dumps({"rows": rows}, ensure_ascii=False).encode()
    assert apply_delta(base, build_delta(base, new)) == new


def test_streaming_responses_pass_through_unbuffered_and_untagged():
    client = _client({"messages": []})
    res = client.get("/api/stream")
    assert res.status_code == 200
    assert "etag" not in res.headers
    assert res.text == "data: 1\n\ndata: 2\n\n"
