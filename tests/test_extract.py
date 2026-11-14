from pathlib import Path

from app.extract import TextDoc, discover, merge_families, _similar

DATA = Path(__file__).resolve().parent.parent / "data"


def _corpus() -> list[TextDoc]:
    templates = [
        "这是纯爱战神啊",
        "纯爱战神太强了",
        "我服了纯爱战神",
        "纯爱战神真的可以",
        "兄弟们纯爱战神",
    ]
    docs = []
    for index in range(40):
        docs.append(
            TextDoc(
                text=templates[index % len(templates)] + f"第{index}个",
                bvid=f"BV{index}",
                zone="鬼畜" if index % 2 else "生活",
                kind="comment",
            )
        )
    docs.append(TextDoc(text="这是什么梗", bvid="BV999", zone="知识", kind="comment"))
    docs.append(TextDoc(text="「退退退」笑死", bvid="BV998", zone="鬼畜", kind="comment"))
    return docs


def test_discover_finds_repeated_phrase():
    candidates = discover(_corpus(), data_dir=DATA)
    surfaces = [c.norm for c in candidates]
    assert any("纯爱战神" in surface for surface in surfaces), surfaces[:20]


def test_discover_extracts_quoted_surface():
    candidates = discover(_corpus(), data_dir=DATA)
    surfaces = [c.norm for c in candidates]
    assert any("退退" in surface for surface in surfaces)


def test_similarity_merges_variants():
    assert _similar("退退", "退退退")
    assert _similar("尊嘟假嘟", "尊嘟假嘟啊")
    assert not _similar("纯爱战神", "打工人")


def test_merge_families_uses_seed_canonical():
    candidates = discover(_corpus(), data_dir=DATA)
    seed_map = {"纯爱战神": {"canonical": "纯爱战神", "type": "词", "origin": "test"}}
    families = merge_families(candidates, seed_map, {})
    keys = [f["family_key"] for f in families]
    assert "纯爱战神" in keys
    family = next(f for f in families if f["family_key"] == "纯爱战神")
    assert family["seed_hit"] is True
    assert family["type"] == "词"
