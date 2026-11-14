from __future__ import annotations

import pytest

from meme_info.evaluate import evaluate, evaluate_discovery, render_markdown


def predictions():
    return [
        {"canonical": "真梗", "score": 90, "signals": {"meta_views": 2_000_000, "organic_docs": 20}},
        {"canonical": "老梗", "score": 70, "signals": {"meta_views": 1_000_000, "organic_docs": 30}},
        {"canonical": "新闻事件", "score": 60, "signals": {"meta_views": 500_000, "organic_docs": 10}},
        {"canonical": "不匹配", "score": 50, "signals": {}},
    ]


def labels():
    return [
        {"canonical": "真梗", "label": "new_meme", "current": True, "heat": 5},
        {"canonical": "老梗", "label": "evergreen", "current": True, "heat": 3},
        {"canonical": "新闻事件", "label": "news_event", "current": False, "heat": 0},
    ]


def test_precision_and_calibration():
    report = evaluate(predictions(), labels(), k=3)
    assert report["precision_at_k"] == pytest.approx(2 / 3, abs=1e-4)
    assert report["current_precision_at_k"] == pytest.approx(2 / 3, abs=1e-4)
    assert report["label_coverage"] == 1.0
    assert report["calibration"]["n"] == 3
    assert report["calibration"]["mae_100"] is not None
    assert "baselines" in report


def test_markdown_renders():
    text = render_markdown(evaluate(predictions(), labels(), k=3))
    assert "precision_at_k" in text
    assert "单信号基线" in text


def test_discovery_metrics():
    report = evaluate_discovery(
        ["尊嘟假嘟", "抽象", "新闻"],
        ["尊嘟假嘟", "抽象", "哈基米"],
        ["新闻"],
        k=3,
    )
    assert report["positive_hits"] == 2
    assert report["negative_hits"] == 1
    assert report["recall_at_k"] == pytest.approx(2 / 3, abs=1e-4)
