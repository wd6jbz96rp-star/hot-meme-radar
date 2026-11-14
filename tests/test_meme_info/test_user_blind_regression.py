from __future__ import annotations

import json
from pathlib import Path

from meme_info.knowledge import Knowledge

REPO = Path(__file__).resolve().parents[2]
BLIND = REPO / "meme_info" / "labels" / "blind_20261114_user.json"


def test_user_blind_file_contract():
    payload = json.loads(BLIND.read_text(encoding="utf-8"))
    items = payload["items"]
    assert len(items) == 16
    labels = {item["canonical"]: item["label"] for item in items}
    assert labels["扎啤"] == "current_meme"
    assert labels["吴家村"] == "current_meme"
    assert labels["最怕BLG夺冠之人"] == "non_meme"
    assert labels["非凡之爱"] == "non_meme"
    assert labels["腋毛攻击"] == "non_meme"
    assert labels["明码标价"] == "non_meme"


def test_correction_does_not_kill_user_confirmed_memes():
    knowledge = Knowledge()
    # PM 修正：扎啤/吴家村不得被普通词/地名一刀切误杀。
    assert knowledge.blocked_reason("扎啤") == ""
    assert knowledge.blocked_reason("吴家村") == ""
    assert knowledge.is_proper_like("吴家村") is True
    # 仍保留用户/PM 确认的普通短语过滤。
    assert knowledge.blocked_reason("明码标价") == "common_phrase"
