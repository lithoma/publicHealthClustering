"""Focused live-PostgreSQL tests for the Chinese knowledge base."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from public_health_clustering.dashboard import app
from public_health_clustering.db import create_db_engine
from public_health_clustering.knowledge_base import search_knowledge, seed_chinese_knowledge_base


@pytest.fixture()
def db_session():
    engine = create_db_engine()
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE knowledge_sections, knowledge_documents RESTART IDENTITY CASCADE"))
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback(); session.close()
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE knowledge_sections, knowledge_documents RESTART IDENTITY CASCADE"))
        engine.dispose()


def test_seed_is_idempotent_and_documents_are_chinese(db_session):
    assert seed_chinese_knowledge_base(db_session) == 3
    db_session.flush()
    assert seed_chinese_knowledge_base(db_session) == 0
    documents = search_knowledge(db_session)
    assert {item.disease for item in documents} == {"麻疹", "百日咳", "流感"}
    assert all(item.sections and item.source_url.startswith("https://") for item in documents)


def test_searches_sections_and_filters_disease(db_session):
    seed_chinese_knowledge_base(db_session); db_session.flush()
    assert [item.disease for item in search_knowledge(db_session, query="孕产妇")] == ["百日咳"]
    assert [item.disease for item in search_knowledge(db_session, disease="麻疹")] == ["麻疹"]


def test_knowledge_api_returns_chinese_content_and_missing_is_clear(db_session):
    seed_chinese_knowledge_base(db_session); db_session.commit()
    client = TestClient(app)
    response = client.get("/api/knowledge?query=通风")
    assert response.status_code == 200
    assert response.json()[0]["disease"] == "流感"
    assert response.json()[0]["sections"][0]["title"]
    assert client.get("/api/knowledge/999999").json()["detail"] == "知识文档不存在"
