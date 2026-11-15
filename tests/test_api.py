import importlib
import json
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient


def _report() -> dict:
    return {
        "schema_version": "meme-info/v1",
        "generated_at": datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(),
        "memes": [{
            "family_key": "k1", "canonical": "测试梗", "score": 66.6, "status": "hot",
            "type": "词", "signals": {"evidence_rate_2x2": True},
            "verified": {"is_meme": True, "is_current": True, "heat": 60, "confidence": 0.7, "reason": "ok"},
            "evidence": [],
        }],
    }


def test_readonly_api_contract(tmp_path, monkeypatch):
    from app import config, meme_info_adapter

    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "MEME_INFO_REPORT_PATH", report)
    meme_info_adapter.clear_cache()

    from app import api as api_module
    api_module = importlib.reload(api_module)
    with TestClient(api_module.app) as client:
        index = client.get("/")
        assert index.status_code == 200 and "网络热梗" in index.text

        body = client.get("/api/meme-info").json()
        assert body["status"] == "ok" and body["count"] == 1

        health = client.get("/api/health").json()
        assert health["ready"] is True and health["scheduler"] is False

        assert client.post("/api/refresh").status_code == 410
        assert client.get("/api/top").status_code == 404
        assert client.get("/api/meme/test/history").status_code == 404
