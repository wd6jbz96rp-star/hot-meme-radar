"""评测：把预测（TopK 梗）与人工/银标标签对齐，算精度、召回、校准与证据质量。

标签不是 ground truth 时，报告必须写清来源。这里不把 LLM 审核结果当标签；
``labels`` 由人维护（或明确标注为 silver），评测只看给定文件。
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable

from .models import Meme
from .text import normalize_surface

MEME_LABELS = {
    "new_meme", "recent_meme", "rising_old", "evergreen", "classic_meme",
    "meme", "current_meme",
}
CURRENT_LABELS = {"new_meme", "recent_meme", "rising_old", "current_meme", "meme"}
NEGATIVE_LABELS = {
    "news_event", "proper_noun", "marketing", "non_meme", "not_meme",
    "event", "person",
}


def _prediction_rows(predictions: Any) -> list[dict]:
    if isinstance(predictions, dict):
        predictions = predictions.get("memes") or predictions.get("predictions") or []
    rows: list[dict] = []
    for item in predictions or []:
        if isinstance(item, Meme):
            rows.append(
                {
                    "family_key": item.family_key,
                    "canonical": item.canonical,
                    "members": list(item.members),
                    "score": float(item.score),
                    "smoothed_score": item.smoothed_score,
                    "status": item.status,
                    "evidence": item.evidence,
                    "signals": dict(item.signals),
                    "meta": item.meta,
                    "usage": item.usage,
                    "components": item.components,
                }
            )
            continue
        payload = dict(item or {})
        rows.append(
            {
                "family_key": normalize_surface(
                    payload.get("family_key") or payload.get("canonical") or payload.get("surface") or ""
                ),
                "canonical": str(payload.get("canonical") or payload.get("surface") or ""),
                "members": list(payload.get("members") or []),
                "score": float(payload.get("score") or payload.get("heat") or 0.0),
                "smoothed_score": payload.get("smoothed_score"),
                "status": str(payload.get("status") or ""),
                "evidence": payload.get("evidence") or [],
                "signals": dict(payload.get("signals") or {}),
                "meta": payload.get("meta") or [],
                "usage": payload.get("usage") or [],
                "components": payload.get("components") or {},
            }
        )
    rows.sort(
        key=lambda item: (
            float(item["smoothed_score"]) if item["smoothed_score"] is not None else float(item["score"]),
            item["canonical"],
        ),
        reverse=True,
    )
    return rows


def _label_rows(labels: Any) -> list[dict]:
    if isinstance(labels, dict):
        if "items" in labels:
            labels = labels["items"]
        elif "labels" in labels:
            labels = labels["labels"]
        else:
            labels = [{"canonical": key, "label": value} for key, value in labels.items()]
    rows: list[dict] = []
    for item in labels or []:
        if isinstance(item, str):
            rows.append({"canonical": item, "label": "meme"})
            continue
        payload = dict(item or {})
        canonical = str(payload.get("canonical") or payload.get("name") or payload.get("surface") or "").strip()
        if not canonical:
            continue
        label = str(payload.get("label") or payload.get("type") or "unclear").strip()
        aliases = [str(alias) for alias in (payload.get("aliases") or []) if str(alias).strip()]
        members = [str(member) for member in (payload.get("members") or []) if str(member).strip()]
        current = payload.get("current")
        if current is None:
            current = label in CURRENT_LABELS
        heat = payload.get("heat")
        heat_100 = payload.get("heat_100")
        if heat_100 is None and heat is not None:
            try:
                heat_100 = float(heat) * 20.0 if float(heat) <= 5 else float(heat)
            except (TypeError, ValueError):
                heat_100 = None
        rows.append(
            {
                "canonical": canonical,
                "label": label,
                "aliases": aliases,
                "members": members,
                "current": bool(current),
                "heat": heat,
                "heat_100": heat_100,
                "confidence": payload.get("confidence"),
                "notes": payload.get("notes"),
            }
        )
    return rows


def _index_labels(labels: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for label in labels:
        keys = [label["canonical"], *label.get("aliases", []), *label.get("members", [])]
        for key in keys:
            norm = normalize_surface(key)
            if norm:
                index.setdefault(norm, label)
    return index


def _match(row: dict, index: dict[str, dict]) -> dict | None:
    keys = [row["family_key"], row["canonical"], *row.get("members", [])]
    for key in keys:
        norm = normalize_surface(key)
        if norm and norm in index:
            return index[norm]
    return None


def _dcg(relevances: list[float]) -> float:
    return sum((2 ** rel - 1) / math.log2(index + 2) for index, rel in enumerate(relevances))


def ndcg_at_k(relevances: list[float], k: int = 10) -> float:
    selected = [max(0.0, float(value)) for value in relevances[:k]]
    ideal = sorted(selected, reverse=True)
    actual = _dcg(selected)
    best = _dcg(ideal)
    return round(actual / best, 4) if best > 0 else 0.0


def spearman(left: list[float], right: list[float]) -> float:
    pairs = [(float(a), float(b)) for a, b in zip(left, right)]
    if len(pairs) < 2:
        return 0.0

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda index: values[index])
        result = [0.0] * len(values)
        index = 0
        while index < len(order):
            end = index
            while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
                end += 1
            average = (index + end) / 2.0
            for position in range(index, end + 1):
                result[order[position]] = average
            index = end + 1
        return result

    rank_left = ranks([pair[0] for pair in pairs])
    rank_right = ranks([pair[1] for pair in pairs])
    mean_left = sum(rank_left) / len(rank_left)
    mean_right = sum(rank_right) / len(rank_right)
    numerator = sum((a - mean_left) * (b - mean_right) for a, b in zip(rank_left, rank_right))
    denominator = math.sqrt(
        sum((a - mean_left) ** 2 for a in rank_left)
        * sum((b - mean_right) ** 2 for b in rank_right)
    )
    return round(numerator / denominator, 4) if denominator > 0 else 0.0


def _row_evidence(row: dict) -> tuple[int, int, int]:
    """2 评论/2 视频门槛只数评论/弹幕；标题/简介提及不算「使用」。"""
    usage = row.get("usage") or []
    adoption = []
    for item in usage:
        kind = getattr(item, "kind", None) if not isinstance(item, dict) else item.get("kind")
        if kind in ("comment", "danmaku"):
            adoption.append(item)
    docs = len({
        getattr(item, "doc_id", None) or (item.get("doc_id") if isinstance(item, dict) else None)
        for item in adoption
    })
    videos = len({
        getattr(item, "bvid", None) or (item.get("bvid") if isinstance(item, dict) else None)
        for item in adoption
    })
    meta = row.get("meta") or []
    return docs, videos, len(meta)


def _metrics_for(
    rows: list[dict],
    index: dict[str, dict],
    *,
    k: int,
) -> dict:
    top = rows[:k]
    matched = [_match(row, index) for row in top]
    labels_found = [item for item in matched if item is not None]
    reached = {id(item): item for item in labels_found}
    total_labels = len({id(item): item for item in index.values()})
    positives = [item for item in {id(x): x for x in index.values()}.values() if item["label"] in MEME_LABELS]
    current = [item for item in {id(x): x for x in index.values()}.values() if item["current"]]
    hit_positives = sum(1 for item in reached.values() if item["label"] in MEME_LABELS)
    hit_current = sum(1 for item in reached.values() if item["current"])

    def relevant(item: dict | None) -> bool:
        return bool(item and item["label"] in MEME_LABELS)

    def current_relevant(item: dict | None) -> bool:
        return bool(item and item["current"])

    precision = sum(1 for item in matched if relevant(item)) / len(top) if top else 0.0
    current_precision = sum(1 for item in matched if current_relevant(item)) / len(top) if top else 0.0
    new_precision = sum(
        1 for item in matched if item and item["label"] in {"new_meme", "recent_meme"}
    ) / len(top) if top else 0.0
    first_relevant = next(
        (position + 1 for position, item in enumerate(matched) if relevant(item)), None
    )
    relevances: list[float] = []
    for item in matched:
        if not item:
            relevances.append(0.0)
        elif item.get("heat_100") is not None:
            relevances.append(max(0.0, float(item["heat_100"]) / 100.0))
        elif item["label"] in MEME_LABELS:
            relevances.append(1.0 if item["current"] else 0.5)
        else:
            relevances.append(0.0)
    evidence = [_row_evidence(row) for row in top]
    score_heat_pairs = [
        (float(row["score"]), float(item["heat_100"]))
        for row, item in zip(top, matched)
        if item and item.get("heat_100") is not None
    ]
    unmatched = [row["canonical"] for row, item in zip(top, matched) if item is None]
    label_counts = Counter(item["label"] for item in labels_found)
    return {
        "k": k,
        "n": len(top),
        "precision_at_k": round(precision, 4),
        "current_precision_at_k": round(current_precision, 4),
        "new_precision_at_k": round(new_precision, 4),
        "mrr": round(1.0 / first_relevant, 4) if first_relevant else 0.0,
        "ndcg_at_k": ndcg_at_k(relevances, k),
        "recall_labels": round(hit_positives / len(positives), 4) if positives else None,
        "recall_current": round(hit_current / len(current), 4) if current else None,
        "label_coverage": round(len(labels_found) / len(top), 4) if top else 0.0,
        "unmatched_rate": round(len(unmatched) / len(top), 4) if top else 0.0,
        "unmatched": unmatched[:10],
        "evidence_rate_2x2": round(
            sum(1 for docs, videos, _meta in evidence if docs >= 2 and videos >= 2) / len(top), 4
        ) if top else 0.0,
        "meta_rate": round(sum(1 for _docs, _videos, meta in evidence if meta) / len(top), 4) if top else 0.0,
        "freshness_7d_rate": round(
            sum(1 for row in top if (row.get("signals") or {}).get("usage_7d")) / len(top), 4
        ) if top else 0.0,
        "label_counts": dict(sorted(label_counts.items())),
        "calibration": {
            "n": len(score_heat_pairs),
            "spearman": spearman(
                [pair[0] for pair in score_heat_pairs],
                [pair[1] for pair in score_heat_pairs],
            ),
            "mae_100": round(
                sum(abs(pair[0] - pair[1]) for pair in score_heat_pairs) / len(score_heat_pairs), 2
            ) if score_heat_pairs else None,
        },
        "total_labels": total_labels,
    }


def evaluate(
    predictions: Any,
    labels: Any,
    *,
    k: int = 10,
    include_baselines: bool = True,
) -> dict:
    rows = _prediction_rows(predictions)
    label_rows = _label_rows(labels)
    index = _index_labels(label_rows)
    result = _metrics_for(rows, index, k=k)
    result["generated_from"] = {
        "predictions": len(rows),
        "labels": len(label_rows),
    }
    if include_baselines:
        baselines: dict[str, dict] = {}
        keyed = {
            "meta_views": lambda row: float((row.get("signals") or {}).get("meta_views") or 0.0),
            "usage_docs": lambda row: float((row.get("signals") or {}).get("organic_docs") or (row.get("signals") or {}).get("usage_docs") or 0.0),
            "recency": lambda row: float((row.get("signals") or {}).get("last_ts") or 0.0),
            "rule_score": lambda row: float(row.get("score") or 0.0),
        }
        for name, getter in keyed.items():
            if name == "rule_score":
                continue
            ordered = sorted(rows, key=getter, reverse=True)
            baselines[name] = _metrics_for(ordered, index, k=k)
        result["baselines"] = baselines
    return result


def evaluate_discovery(
    predicted: Iterable[str],
    positives: Iterable[str],
    negatives: Iterable[str],
    *,
    k: int = 50,
) -> dict:
    """候选发现评测：黄金正例召回与负例误入。"""
    predicted_norms = [normalize_surface(item) for item in predicted]
    top = predicted_norms[:k]
    positive_norms = [normalize_surface(item) for item in positives]
    negative_norms = [normalize_surface(item) for item in negatives]
    positive_hits = [item for item in positive_norms if item in top]
    negative_hits = [item for item in negative_norms if item in top]
    return {
        "k": k,
        "positives": len(positive_norms),
        "negatives": len(negative_norms),
        "positive_hits": len(positive_hits),
        "negative_hits": len(negative_hits),
        "recall_at_k": round(len(positive_hits) / len(positive_norms), 4) if positive_norms else None,
        "false_positive_rate": round(len(negative_hits) / len(negative_norms), 4) if negative_norms else None,
        "positive_hit_items": positive_hits[:20],
        "negative_hit_items": negative_hits[:20],
    }


def render_markdown(report: dict) -> str:
    lines = ["## 评测总览", "", "| 指标 | 值 |", "|---|---|"]
    for key in (
        "precision_at_k", "current_precision_at_k", "new_precision_at_k",
        "mrr", "ndcg_at_k", "recall_labels", "recall_current",
        "label_coverage", "evidence_rate_2x2", "meta_rate", "freshness_7d_rate",
    ):
        lines.append(f"| {key} | {report.get(key)} |")
    calibration = report.get("calibration") or {}
    lines.append(f"| spearman | {calibration.get('spearman')} |")
    lines.append(f"| mae_100 | {calibration.get('mae_100')} |")
    if report.get("unmatched"):
        lines.append("")
        lines.append("未匹配标签的 TopK：" + "、".join(report["unmatched"]))
    baselines = report.get("baselines") or {}
    if baselines:
        lines += ["", "## 单信号基线（同一批候选）", "", "| 基线 | P@K | current P@K | nDCG@K |", "|---|---|---|---|"]
        for name, item in baselines.items():
            lines.append(
                f"| {name} | {item.get('precision_at_k')} | "
                f"{item.get('current_precision_at_k')} | {item.get('ndcg_at_k')} |"
            )
    return "\n".join(lines)
