"""B站分区配置：按"造梗密度"排序并赋权。"""
from __future__ import annotations

# rid 为 B 站一级分区 id；weight 用于曝光分加权（1.0 为基准）
ZONES: list[dict] = [
    {"rid": 119, "name": "鬼畜", "weight": 1.4},
    {"rid": 160, "name": "生活", "weight": 1.2},
    {"rid": 1, "name": "动画", "weight": 1.1},
    {"rid": 4, "name": "游戏", "weight": 1.1},
    {"rid": 181, "name": "影视", "weight": 1.0},
    {"rid": 3, "name": "音乐", "weight": 0.9},
    {"rid": 5, "name": "娱乐", "weight": 0.9},
    {"rid": 36, "name": "知识", "weight": 0.7},
]

ZONE_BY_RID = {z["rid"]: z for z in ZONES}
ZONE_WEIGHT_BY_NAME = {z["name"]: z["weight"] for z in ZONES}


def zone_weight(rid: int | None, tname: str | None = None) -> float:
    """按 rid 或分区名取权重，未知分区给 0.6。"""
    if rid in ZONE_BY_RID:
        return ZONE_BY_RID[rid]["weight"]
    if tname:
        for name, weight in ZONE_WEIGHT_BY_NAME.items():
            if tname.startswith(name):
                return weight
    return 0.6


def zone_name(rid: int | None, tname: str | None = None) -> str:
    if rid in ZONE_BY_RID:
        return ZONE_BY_RID[rid]["name"]
    return tname or "其他"
