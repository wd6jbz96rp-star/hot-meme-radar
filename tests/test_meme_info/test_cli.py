from __future__ import annotations

import json
from pathlib import Path

import pytest

from meme_info import cli

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"


def test_offline_query_end_to_end(tmp_path, monkeypatch):
    out = tmp_path / "report.json"
    code = cli.main([
        "query",
        "--offline-fixtures", str(FIXTURES),
        "--out", str(out),
        "--now", "2025-04-10T12:00:00+08:00",
        "--comment-videos", "3",
        "--danmaku-videos", "1",
        "--max-requests", "30",
        "--no-search",
        "--no-expand",
        "--limit", "10",
        "--quiet",
    ])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"]
    assert "memes" in payload
    assert "candidates" in payload
    assert "stats" in payload


def test_offline_collect_and_score(tmp_path):
    corpus_path = tmp_path / "corpus.json"
    code = cli.main([
        "collect",
        "--offline-fixtures", str(FIXTURES),
        "--out", str(corpus_path),
        "--now", "2025-04-10T12:00:00+08:00",
        "--comment-videos", "3",
        "--danmaku-videos", "1",
        "--max-requests", "30",
        "--no-search",
        "--quiet",
    ])
    assert code == 0
    report = json.loads(corpus_path.read_text(encoding="utf-8"))
    assert report["videos"]
    assert report["docs"]
    code = cli.main([
        "score", "--corpus", str(corpus_path), "--now", "2025-04-10T12:00:00+08:00",
        "--out", str(tmp_path / "score.json"), "--limit", "5", "--quiet",
    ])
    assert code == 0


def test_review_without_key_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "MEME_INFO_LLM_API_KEY", "MEME_INFO_LLM_API_KEY_FILE",
        "HOTMEME_LLM_API_KEY", "HOTMEME_LLM_API_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    candidates = tmp_path / "candidates.json"
    candidates.write_text('{"candidates": [{"canonical": "测试梗", "surface": "测试梗"}]}', encoding="utf-8")
    code = cli.main(["review", "--candidates", str(candidates), "--quiet"])
    assert code == 2


def test_eval_cli(tmp_path):
    report = {
        "memes": [
            {"canonical": "真梗", "score": 80, "signals": {}},
            {"canonical": "新闻", "score": 70, "signals": {}},
        ]
    }
    labels = {
        "items": [
            {"canonical": "真梗", "label": "new_meme", "current": True, "heat": 4},
            {"canonical": "新闻", "label": "news_event", "current": False},
        ]
    }
    pred = tmp_path / "pred.json"
    label = tmp_path / "labels.json"
    out = tmp_path / "eval.json"
    pred.write_text(json.dumps(report), encoding="utf-8")
    label.write_text(json.dumps(labels), encoding="utf-8")
    assert cli.main(["eval", "--pred", str(pred), "--labels", str(label), "--k", "2", "--out", str(out)]) == 0
    metrics = json.loads(out.read_text(encoding="utf-8"))
    assert metrics["precision_at_k"] == 0.5


def test_module_does_not_import_app():
    for path in (REPO / "meme_info").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import app" not in text, path
        assert "from app" not in text, path
