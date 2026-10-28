"""DEBUG archive browser: private, validated, paged, and read only."""
import json
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from agent_core import config
from agent_core.core.history_context import _digest, _json
from agent_core.core.storage import SessionStore
from agent_frontend.web.app import create_app


@pytest.fixture
def debug_archive_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEBUG", True)
    monkeypatch.setattr(config, "ENABLE_AGENT_TASKS", False)
    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", False)
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    try:
        with TestClient(
            create_app(store=store, token="archive-token"),
            base_url="http://localhost",
            headers={"Authorization": "Bearer archive-token"},
        ) as client:
            yield client, store
    finally:
        store.close()


def _archive(store, session_id, count=2, *, summary="session summary", raw_text="private history"):
    folder = store.directory(session_id) / "context"
    chunks_dir = folder / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    for index in range(count):
        identifier = f"{index + 1:032x}"
        history = [{"role": "user", "content": f"{raw_text} {index}"}]
        digest = _digest(history)
        record = {
            "ID": identifier,
            "Time": {"start": index + 1, "end": index + 2, "created_at": "2026-01-01T00:00:00Z"},
            "Summary": f"chunk summary {index}",
            "summary_kind": "model",
            "RawHistory": _json(history),
            "digest": digest,
            "session_id": session_id,
        }
        (chunks_dir / f"{identifier}.json").write_text(_json(record), encoding="utf-8")
        chunks.append({key: value for key, value in record.items() if key not in {"RawHistory", "session_id"}})
    manifest = {
        "version": 1, "owner_id": session_id, "revision": 3,
        "session_summary": summary, "summary_kind": "model",
        "chunks": chunks, "updated_at": "2026-01-01T00:00:00Z",
    }
    (folder / "manifest.json").write_text(_json(manifest), encoding="utf-8")
    return folder, chunks


def test_missing_manifest_reports_empty_archive_and_paths(debug_archive_client):
    client, store = debug_archive_client
    session_id = client.post("/api/sessions", json={"title": "empty"}).json()["id"]
    response = client.get(f"/api/debug/sessions/{session_id}/context")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["format"] == "json"
    assert data["revision"] == 0
    assert data["chunks"] == []
    assert data["total_chunks"] == 0
    assert data["next_offset"] is None
    root = store.directory(session_id)
    assert data["paths"] == {
        "manifest": str(root / "context" / "manifest.json"),
        "chunks": str(root / "context" / "chunks"),
        "journal": str(root / "messages.jsonl"),
    }


def test_listing_pages_metadata_without_reading_raw_chunks(debug_archive_client, monkeypatch):
    client, store = debug_archive_client
    session_id = client.post("/api/sessions", json={"title": "paged"}).json()["id"]
    _, chunks = _archive(store, session_id, count=3)

    def forbidden(*_args, **_kwargs):
        pytest.fail("listing or DEBUG snapshot read archived raw history")

    from agent_test.web.context_archive import ContextArchiveReader
    monkeypatch.setattr(ContextArchiveReader, "_read_records", forbidden)
    assert "private history" not in client.get("/api/debug").text
    response = client.get(f"/api/debug/sessions/{session_id}/context", params={"offset": 1, "limit": 1})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["revision"] == 3
    assert data["session_summary"] == "session summary"
    assert data["summary_kind"] == "model"
    assert data["total_chunks"] == 3
    assert data["next_offset"] == 2
    assert data["chunks"] == [{
        "ID": chunks[1]["ID"], "Time": chunks[1]["Time"],
        "Summary": chunks[1]["Summary"], "summary_kind": "model",
        "path": str(store.directory(session_id) / "context" / "chunks" / f"{chunks[1]['ID']}.json"),
    }]
    assert "RawHistory" not in response.text


def test_chunk_raw_history_pages_and_reassembles_exact_json(debug_archive_client):
    client, store = debug_archive_client
    session_id = client.post("/api/sessions", json={"title": "raw"}).json()["id"]
    _, chunks = _archive(store, session_id, count=1, raw_text="abc 你好 xyz")
    url = f"/api/debug/sessions/{session_id}/context/{chunks[0]['ID']}"
    pieces = []
    offset = 0
    while True:
        response = client.get(url, params={"offset": offset, "limit": 5})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["ID"] == chunks[0]["ID"]
        assert data["Summary"] == "chunk summary 0"
        assert data["summary_kind"] == "model"
        assert data["raw_chars"] > 5
        pieces.append(data["RawHistory"])
        if data["next_offset"] is None:
            break
        assert data["next_offset"] > offset
        offset = data["next_offset"]
    assert json.loads("".join(pieces)) == [{"role": "user", "content": "abc 你好 xyz 0"}]


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 21}, {"offset": -1},
])
def test_listing_rejects_invalid_paging(debug_archive_client, params):
    client, _ = debug_archive_client
    session_id = client.post("/api/sessions", json={"title": "invalid"}).json()["id"]
    assert client.get(f"/api/debug/sessions/{session_id}/context", params=params).status_code == 422


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 6001}, {"offset": -1},
])
def test_chunk_rejects_invalid_paging(debug_archive_client, params):
    client, store = debug_archive_client
    session_id = client.post("/api/sessions", json={"title": "invalid"}).json()["id"]
    _, chunks = _archive(store, session_id, count=1)
    url = f"/api/debug/sessions/{session_id}/context/{chunks[0]['ID']}"
    assert client.get(url, params=params).status_code == 422


def test_archive_routes_require_debug_token_and_local_origin(debug_archive_client):
    client, store = debug_archive_client
    session_id = client.post("/api/sessions", json={"title": "private"}).json()["id"]
    _, chunks = _archive(store, session_id, count=1)
    urls = [
        f"/api/debug/sessions/{session_id}/context",
        f"/api/debug/sessions/{session_id}/context/{chunks[0]['ID']}",
    ]
    for url in urls:
        assert client.get(url, headers={"Authorization": ""}).status_code == 401
        assert client.get(url, headers={"Host": "evil.example"}).status_code == 403
        assert client.get(url, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get(url, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_archive_routes_are_hidden_when_debug_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEBUG", False)
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    try:
        with TestClient(create_app(store=store), base_url="http://localhost") as client:
            session_id = client.post("/api/sessions", json={"title": "hidden"}).json()["id"]
            _archive(store, session_id, count=1)
            urls = [
                f"/api/debug/sessions/{session_id}/context",
                f"/api/debug/sessions/{session_id}/context/{1:032x}",
            ]
            for url in urls:
                assert client.get(url).status_code == 404
    finally:
        store.close()


def test_chunk_cannot_cross_session_or_accept_path_injection(debug_archive_client):
    client, store = debug_archive_client
    owner = client.post("/api/sessions", json={"title": "owner"}).json()["id"]
    other = client.post("/api/sessions", json={"title": "other"}).json()["id"]
    _, chunks = _archive(store, owner, count=1)
    foreign = client.get(f"/api/debug/sessions/{other}/context/{chunks[0]['ID']}")
    assert foreign.status_code in {403, 404}
    assert "private history" not in foreign.text
    for bad in ("not-hex", f"{chunks[0]['ID']}.json", ".."):
        response = client.get(f"/api/debug/sessions/{owner}/context/{bad}")
        assert response.status_code >= 400
        assert "private history" not in response.text


def test_tampered_digest_and_owner_never_reveal_raw_history(debug_archive_client):
    client, store = debug_archive_client
    session_id = client.post("/api/sessions", json={"title": "tamper"}).json()["id"]
    folder, chunks = _archive(store, session_id, count=1)
    url = f"/api/debug/sessions/{session_id}/context/{chunks[0]['ID']}"
    path = folder / "chunks" / f"{chunks[0]['ID']}.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    for change in ({"RawHistory": '[{"role":"user","content":"forged secret"}]'},
                   {"session_id": "another-owner"}):
        path.write_text(_json({**original, **change}), encoding="utf-8")
        response = client.get(url)
        assert response.status_code >= 400
        assert "forged secret" not in response.text
    path.write_text(_json(original), encoding="utf-8")


def test_symlink_and_hardlink_archive_files_are_rejected(debug_archive_client, tmp_path):
    client, store = debug_archive_client
    session_id = client.post("/api/sessions", json={"title": "links"}).json()["id"]
    folder, chunks = _archive(store, session_id, count=1)
    url = f"/api/debug/sessions/{session_id}/context/{chunks[0]['ID']}"
    chunk_path = folder / "chunks" / f"{chunks[0]['ID']}.json"
    outside = tmp_path / "outside.json"
    outside.write_text(chunk_path.read_text(encoding="utf-8"), encoding="utf-8")
    chunk_path.unlink()
    chunk_path.symlink_to(outside)
    response = client.get(url)
    assert response.status_code >= 400
    assert "private history" not in response.text
    chunk_path.unlink()
    os.link(outside, chunk_path)
    response = client.get(url)
    assert response.status_code >= 400
    assert "private history" not in response.text


def test_credentials_are_redacted_before_chunk_paging(debug_archive_client, monkeypatch):
    secret = "archive-secret-123456"
    monkeypatch.setattr(config, "API_KEY", secret)
    client, store = debug_archive_client
    session_id = client.post("/api/sessions", json={"title": "redaction"}).json()["id"]
    folder, chunks = _archive(store, session_id, count=1,
                              summary=f"summary {secret}", raw_text=f"prefix {secret} suffix")
    listing = client.get(f"/api/debug/sessions/{session_id}/context")
    assert listing.status_code == 200, listing.text
    assert secret not in listing.text
    url = f"/api/debug/sessions/{session_id}/context/{chunks[0]['ID']}"
    offset = 0
    pages = []
    while True:
        response = client.get(url, params={"offset": offset, "limit": 4})
        assert response.status_code == 200, response.text
        assert secret not in response.text
        data = response.json()
        pages.append(data["RawHistory"])
        if data["next_offset"] is None:
            break
        offset = data["next_offset"]
    assert secret not in "".join(pages)
    assert secret in (folder / "chunks" / f"{chunks[0]['ID']}.json").read_text(encoding="utf-8")

def test_manifest_owner_and_link_boundaries_are_enforced(debug_archive_client, tmp_path):
    client, store = debug_archive_client
    session_id = client.post("/api/sessions", json={"title": "manifest"}).json()["id"]
    folder, _ = _archive(store, session_id, count=1)
    manifest_path = folder / "manifest.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(_json({**original, "owner_id": "other-owner"}), encoding="utf-8")
    response = client.get(f"/api/debug/sessions/{session_id}/context")
    assert response.status_code >= 400
    assert "private history" not in response.text

    outside = tmp_path / "outside-manifest.json"
    outside.write_text(_json(original), encoding="utf-8")
    manifest_path.unlink()
    manifest_path.symlink_to(outside)
    response = client.get(f"/api/debug/sessions/{session_id}/context")
    assert response.status_code >= 400

    manifest_path.unlink()
    os.link(outside, manifest_path)
    response = client.get(f"/api/debug/sessions/{session_id}/context")
    assert response.status_code >= 400

def test_runtime_model_and_supervisor_keys_are_redacted_before_paging(debug_archive_client):
    from types import SimpleNamespace

    client, store = debug_archive_client
    manager = client.app.state.manager
    runtime_secret = "runtime-archive-key-987654"
    supervisor_secret = "supervisor-archive-key-765432"
    manager.model_registry.add(
        name="Archive test model", model="mock-model",
        base_url="https://example.invalid/v1", api_key=runtime_secret,
    )

    async def close_context():
        pass

    manager.history_context = SimpleNamespace(
        supervisor=SimpleNamespace(settings=SimpleNamespace(api_key=supervisor_secret)),
        close=close_context,
    )
    session_id = client.post("/api/sessions", json={"title": "dynamic secrets"}).json()["id"]
    raw_text = f"runtime={runtime_secret} supervisor={supervisor_secret}"
    folder, chunks = _archive(
        store, session_id, count=1,
        summary=f"session {runtime_secret} {supervisor_secret}",
        raw_text=raw_text,
    )
    listing = client.get(f"/api/debug/sessions/{session_id}/context")
    assert listing.status_code == 200, listing.text
    url = f"/api/debug/sessions/{session_id}/context/{chunks[0]['ID']}"
    offset = 0
    pages = []
    while True:
        response = client.get(url, params={"offset": offset, "limit": 7})
        assert response.status_code == 200, response.text
        pages.append(response.json()["RawHistory"])
        if response.json()["next_offset"] is None:
            break
        assert response.json()["next_offset"] > offset
        offset = response.json()["next_offset"]
    for secret in (runtime_secret, supervisor_secret):
        assert secret not in listing.text
        assert secret not in "".join(pages)
        assert secret in (folder / "manifest.json").read_text(encoding="utf-8")
        assert secret in (folder / "chunks" / f"{chunks[0]['ID']}.json").read_text(encoding="utf-8")


def test_summary_is_redacted_before_length_limit(debug_archive_client):
    client, store = debug_archive_client
    manager = client.app.state.manager
    secret = "runtime-boundary-secret-123456"
    manager.model_registry.add(
        name="Boundary test model", model="mock-model",
        base_url="https://example.invalid/v1", api_key=secret,
    )
    session_id = client.post("/api/sessions", json={"title": "boundary"}).json()["id"]
    boundary_summary = "x" * 15990 + secret + " ending"
    folder, chunks = _archive(store, session_id, count=1, summary=boundary_summary)
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["chunks"][0]["Summary"] = boundary_summary
    manifest_path.write_text(_json(manifest), encoding="utf-8")
    chunk_path = folder / "chunks" / f"{chunks[0]['ID']}.json"
    record = json.loads(chunk_path.read_text(encoding="utf-8"))
    record["Summary"] = boundary_summary
    chunk_path.write_text(_json(record), encoding="utf-8")

    listing = client.get(f"/api/debug/sessions/{session_id}/context")
    detail = client.get(f"/api/debug/sessions/{session_id}/context/{chunks[0]['ID']}")
    assert listing.status_code == 200, listing.text
    assert detail.status_code == 200, detail.text
    for exposed in (
        listing.json()["session_summary"],
        listing.json()["chunks"][0]["Summary"],
        detail.json()["Summary"],
    ):
        assert secret not in exposed
        assert secret[:10] not in exposed
    assert secret in manifest_path.read_text(encoding="utf-8")
    assert secret in chunk_path.read_text(encoding="utf-8")