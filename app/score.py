"""热度打分：分位归一化 + 加权 + 时间衰减 + EMA 平滑。"""
from __future__ import annotations

import math
from datetime import datetime

from . import config


def percentiles(values: list[float]) -> list[float]:
    """返回每个值在批次内的百分位（0-1），并列取平均名次。"""
    size = len(values)
    if size == 0:
        return []
    if size == 1:
        return [1.0]
    order = sorted(range(size), key=lambda i: values[i])
    ranks = [0.0] * size
    index = 0
    while index < size:
        end = index
        while end + 1 < size and values[order[end + 1]] == values[order[index]]:
            end += 1
        average_rank = (index + end) / 2.0 / (size - 1)
        for position in range(index, end + 1):
            ranks[order[position]] = average_rank
        index = end + 1
    return ranks


def _age_hours(first_seen: str | None, now: datetime) -> float:
    if not first_seen:
        return 0.0
    try:
        when = datetime.fromisoformat(first_seen)
    except (TypeError, ValueError):
        return 0.0
    if when.tzinfo is None:
        when = when.astimezone()
    return max(0.0, (now - when).total_seconds() / 3600.0)


def score_families(families: list[dict], prev_scores: dict[str, float],
                   now: datetime | None = None, limit: int | None = None) -> list[dict]:
    """输入家族及其原始信号，输出排序后的打分结果。"""
    now = now or datetime.now().astimezone()
    entries = [f for f in families if f.get("signals")]
    if not entries:
        return []

    exposure = [float(f["signals"].get("exposure", 0.0)) for f in entries]
    engagement = [float(f["signals"].get("engagement", 0.0)) for f in entries]
    breadth_video = [float(f.get("df_video", 0)) for f in entries]
    breadth_zone = [float(f.get("df_zone", 0)) for f in entries]
    breadth_up = [float(f.get("df_up", 0)) for f in entries]
    velocity = [min(20.0, float(f["signals"].get("velocity", 1.0))) for f in entries]

    p_exposure = percentiles(exposure)
    p_engagement = percentiles(engagement)
    p_video = percentiles(breadth_video)
    p_zone = percentiles(breadth_zone)
    p_up = percentiles(breadth_up)
    p_velocity = percentiles(velocity)

    items: list[dict] = []
    for index, family in enumerate(entries):
        breadth = (p_video[index] + p_zone[index] + p_up[index]) / 3.0
        meme = float(family["signals"].get("meme", 0.0))
        base = (
            config.WEIGHTS["exposure"] * p_exposure[index]
            + config.WEIGHTS["engagement"] * p_engagement[index]
            + config.WEIGHTS["breadth"] * breadth
            + config.WEIGHTS["velocity"] * p_velocity[index]
            + config.WEIGHTS["meme"] * meme
        )
        age = _age_hours(family.get("first_seen"), now)
        decay = math.pow(0.5, age / config.HALF_LIFE_HOURS)
        novelty = 1.0 if age < 72 else 0.0
        raw_score = 100.0 * base * decay * (0.9 + 0.1 * novelty)

        concentration = float(family["signals"].get("concentration", 0.0))
        if concentration > 0.8:
            raw_score *= 0.5
        previous_value = prev_scores.get(family["family_key"])
        previous = float(previous_value or 0.0)
        score = raw_score if previous_value is None else 0.6 * raw_score + 0.4 * previous

        status = "hot" if score >= config.MIN_SCORE else "observing"
        items.append(
            {
                **{k: family[k] for k in
                   ("family_key", "canonical", "type", "origin", "first_seen",
                    "seed_hit", "members", "sample") if k in family},
                "score": round(score, 2),
                "status": status,
                "components": {
                    "exposure": round(p_exposure[index], 3),
                    "engagement": round(p_engagement[index], 3),
                    "breadth": round(breadth, 3),
                    "velocity": round(p_velocity[index], 3),
                    "meme": round(meme, 3),
                    "decay": round(decay, 3),
                    "concentration": round(concentration, 3),
                    "raw": round(raw_score, 2),
                    "previous": round(previous, 2),
                },
                "raw_signals": {
                    "exposure": round(exposure[index], 1),
                    "engagement": round(engagement[index], 1),
                    "lift": round(velocity[index], 2),
                },
            }
        )

    items.sort(key=lambda item: item["score"], reverse=True)

    display: list[dict] = []
    for item in items:
        if item["score"] < config.OBSERVE_MIN:
            item["rank"] = 0
            continue
        if len(display) < (limit or config.TOP_N):
            display.append(item)
    for position, item in enumerate(display, start=1):
        item["rank"] = position
    return items
