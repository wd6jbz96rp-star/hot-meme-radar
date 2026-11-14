from __future__ import annotations

import json

from meme_info.models import Comment, Meme, MetaMention, Usage, Evidence
from meme_info.text import normalize_surface
from meme_info.verify import (
    ChatClient,
    LlmConfig,
    SemanticVerifier,
    extract_candidates,
    review_candidates,
)

from conftest import DAY, NOW


def make_meme(canonical: str = "测试梗") -> Meme:
    norm = normalize_surface(canonical)
    usage = [
        Usage(doc_id="D1", bvid="BVC1", kind="comment", ctime=NOW - DAY, like=10, text=canonical),
        Usage(doc_id="D2", bvid="BVC2", kind="comment", ctime=NOW - 2 * DAY, like=8, text=canonical),
    ]
    meta = [
        MetaMention(surface=canonical, canonical=canonical, norm=norm, bvid="BVM",
                    title=f"{canonical}是什么梗", pubdate=NOW - DAY, view=1_000_000,
                    kind="dedicated", confidence=0.9)
    ]
    evidence = [
        Evidence(comment_id="D1", quote=canonical, ctime=NOW - DAY, like=10, bvid="BVC1"),
    ]
    return Meme(
        family_key=norm, canonical=canonical, norm=norm, members=[canonical],
        usage=usage, meta=meta, evidence=evidence, known=False, meme_prob=0.8,
        first_seen=NOW - 2 * DAY, last_seen=NOW - DAY,
    )


class FakeClient(ChatClient):
    def __init__(self, responses, config=None):
        super().__init__(config or LlmConfig(api_key="sk-test", max_calls=10))
        self.responses = list(responses)
        self.calls_seen = 0

    def _post(self, payload):
        self.calls_seen += 1
        content = self.responses.pop(0) if self.responses else "{}"
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


def verify_payload(**overrides):
    payload = {
        "is_meme": True,
        "is_current": True,
        "heat": 70,
        "confidence": 0.8,
        "reason": "有专门梗视频且近期仍在使用",
        "evidence_ids": ["video:BVM", "D1"],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_verifier_parses_valid_json_and_keeps_evidence_subset():
    client = FakeClient([verify_payload(evidence_ids=["D1", "not-an-id"])])
    verifier = SemanticVerifier(client)
    result = verifier.verify_meme(make_meme(), now=NOW)
    assert result.is_meme is True
    assert result.is_current is True
    assert result.heat == 70
    assert result.evidence_ids == ["D1"]
    assert result.error == ""


def test_verifier_bad_json_returns_error_without_raising():
    client = FakeClient(["not json", "still not json"])
    verifier = SemanticVerifier(client)
    result = verifier.verify_meme(make_meme(), now=NOW)
    assert result.error == "bad_json"
    assert result.is_meme is False


def test_extract_candidates_validates_surface_substring():
    comments = [
        Comment(id="C1", bvid="BVC", text="这个测试梗太好笑了", ctime=NOW, like=10),
        Comment(id="C2", bvid="BVC", text="测试梗 打卡", ctime=NOW, like=5),
    ]
    payload = {
        "candidates": [
            {
                "surface": "测试梗",
                "canonical": "测试梗",
                "type": "词",
                "meaning": "测试用梗",
                "origin_quote": "测试梗",
                "confidence": 0.8,
                "evidence_ids": ["C1", "C2"],
            },
            {
                "surface": "编造的梗",
                "canonical": "编造的梗",
                "type": "词",
                "confidence": 0.9,
                "evidence_ids": ["C1"],
            },
        ]
    }
    client = FakeClient([json.dumps(payload, ensure_ascii=False)])
    result = extract_candidates(comments, client, max_calls=1, now=NOW)
    assert result["reason"] == "ok"
    assert [item["surface"] for item in result["candidates"]] == ["测试梗"]
    assert result["candidates"][0]["evidence_ids"] == ["C1", "C2"]


def test_review_requires_two_comments_and_two_videos():
    client = FakeClient([verify_payload()])
    result = review_candidates([make_meme()], client, max_calls=1, target=1, now=NOW)
    assert len(result["approved"]) == 1
    assert result["reason"] == "ok"
    assert result["calls"] == 1


def test_not_configured_returns_explicit_reason():
    client = ChatClient(LlmConfig(api_key=""))
    result = review_candidates([make_meme()], client, now=NOW)
    assert result["reason"] == "not_configured"
    assert result["calls"] == 0
    extraction = extract_candidates([{"id": "1", "text": "测试梗", "ctime": NOW}], client, now=NOW)
    assert extraction["reason"] == "not_configured"
    assert extraction["candidates"] == []


def test_budget_exhausted_does_not_crash():
    client = FakeClient([verify_payload()], config=LlmConfig(api_key="sk-test", max_calls=0))
    extraction = extract_candidates([{"id": "1", "text": "测试梗", "ctime": NOW}], client, now=NOW)
    assert extraction["candidates"] == []
    assert extraction["reason"] == "budget"
