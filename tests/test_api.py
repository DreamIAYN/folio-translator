from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import Database, new_id
from app.library import ReferenceLibrary
from app.service import TranslationService


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = replace(
        main.settings,
        database_path=tmp_path / "api.db",
        upload_root=tmp_path / "uploads",
        export_root=tmp_path / "exports",
        reference_books_dir=tmp_path / "reference",
        demo_email="demo@test.local",
        demo_password="test-password",
        harness_api_key="test-key",
    )
    settings.upload_root.mkdir(parents=True)
    settings.export_root.mkdir(parents=True)
    settings.reference_books_dir.mkdir(parents=True)
    db = Database(settings.database_path)
    service = TranslationService(settings, db)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "service", service)
    monkeypatch.setattr(main, "library", ReferenceLibrary(settings))
    with TestClient(main.app) as client:
        yield client, db
    service.shutdown(wait=True)


def test_auth_and_empty_shelf(api) -> None:
    client, _db = api
    assert client.get("/api/documents").status_code == 401
    response = client.post(
        "/api/auth/register",
        json={"name": "Reader", "email": "reader@example.com", "password": "long-enough"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["email"] == "reader@example.com"
    me = client.get("/api/auth/me").json()
    assert me["user"]["name"] == "Reader"
    assert client.get("/api/documents").json() == {"items": []}
    library = client.get("/api/library").json()
    assert library["items"] == []
    assert client.post("/api/auth/logout").json() == {"ok": True}
    assert client.get("/api/documents").status_code == 401


def test_document_detail_and_segments(api) -> None:
    client, db = api
    client.post(
        "/api/auth/login",
        json={"email": "demo@test.local", "password": "test-password"},
    )
    user = client.get("/api/auth/me").json()["user"]
    doc_id = new_id("doc")
    db.create_document(
        id=doc_id,
        user_id=user["id"],
        title="Sample",
        author="Author",
        source_lang="en",
        target_lang="zh-CN",
        domain="math",
        style="publish",
        filename="sample.txt",
        ext=".txt",
        size_bytes=100,
        page_count=1,
        char_count=12,
        word_count=2,
        segment_count=2,
        status="completed",
        metadata_json="{}",
        analysis_json='{"title_translation":"示例","summary":"summary"}',
    )
    db.add_segments(
        doc_id,
        [
            {
                "idx": 0,
                "chapter": "1",
                "block_type": "heading",
                "source_text": "Graph",
                "translated_text": "图",
            },
            {
                "idx": 1,
                "chapter": "1",
                "block_type": "paragraph",
                "source_text": "A graph is a pair.",
                "translated_text": "图是一个二元组。",
            },
        ],
    )
    db.create_job(
        document_id=doc_id,
        user_id=user["id"],
        status="completed",
        stage="completed",
        progress=100,
        total_segments=2,
        translated_segments=2,
        model="test",
        tokens_prompt=10,
        tokens_completion=20,
        tokens_total=30,
    )
    detail = client.get(f"/api/documents/{doc_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["analysis"]["title_translation"] == "示例"
    assert payload["tokens_total"] == 30
    updated = client.patch(
        f"/api/documents/{doc_id}/prompt",
        json={"prompt": "保留英文术语对照", "thinking": False},
    )
    assert updated.status_code == 200
    assert updated.json()["custom_prompt"] == "保留英文术语对照"
    assert updated.json()["document"]["custom_prompt"] == "保留英文术语对照"
    segments = client.get(f"/api/documents/{doc_id}/segments?limit=1").json()
    assert segments["total"] == 2 and len(segments["items"]) == 1
    assert segments["has_more"] is True
    search = client.get(f"/api/documents/{doc_id}/segments?q=二元组").json()
    assert search["total"] == 1


def test_unsupported_upload_is_rejected(api) -> None:
    client, _db = api
    client.post(
        "/api/auth/login",
        json={"email": "demo@test.local", "password": "test-password"},
    )
    response = client.post(
        "/api/documents",
        files={"file": ("bad.exe", b"MZ", "application/octet-stream")},
        data={"title": "Bad", "target_lang": "zh-CN"},
    )
    assert response.status_code == 422
    assert "不支持" in response.json()["detail"]
