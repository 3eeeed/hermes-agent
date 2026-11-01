"""Conditional GET for repeated Desktop JSON polls (ETag / 304)."""

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from hermes_cli.web_etag import JsonETagMiddleware


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


def test_streaming_responses_pass_through_unbuffered_and_untagged():
    client = _client({"messages": []})
    res = client.get("/api/stream")
    assert res.status_code == 200
    assert "etag" not in res.headers
    assert res.text == "data: 1\n\ndata: 2\n\n"
