import importlib

from fastapi.testclient import TestClient


def test_api_top_health_and_index(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    from app import api as api_module

    api_module = importlib.reload(api_module)
    with TestClient(api_module.app) as client:
        response = client.get("/api/top")
        assert response.status_code == 200
        body = response.json()
        assert "top" in body and isinstance(body["top"], list)

        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["ready"] is False

        index = client.get("/")
        assert index.status_code == 200
        assert "网络热梗" in index.text
