# 热梗信息模块 · 工程交接单

> 给另一个工程师会话。目标：**先把“热梗信息模块”的工具独立做出来，不接主应用**；最后由 PM/架构会话按架构图组装。
> 当前代码基线：`9730859`。架构图：`.archify/architecture-hotmeme-20261114-181902/hotmeme-architecture.html`。
> 当前质量结论：真实 DeepSeek 3 轮 shadow 只 approve 1 条且偏泛；`new` 榜证据薄（多数候选 1 评论/1 视频）。**不要假设现有实现已经可用。**

## 1. 边界（重要）

- 新建独立目录：`meme_info/`（库）+ `tools/`（CLI），自带 `tests/`。
- **不要修改** `app/`、`app/static/`、`data/`、`docs/01-02`、`README.md`；不要动 8000 端口上的进程。
- 模块**不得** import `app.api` / `app.store` / `app.pipeline`；不要写 SQLite、不要起 FastAPI。
- 只读参考现有实现：`app/llm_extract.py`、`app/review.py`、`app/llm.py`、`eval/`。
- 不联网跑单测；真机验证只写 `/tmp`；key 不写进仓库、不打印。

## 2. 模块职责（只做这四件事）

```
评论采集/采样 → LLM 候选抽取 → LLM 审核/verdict → 评测
```

不做：API、SSE、看板、调度器、数据库、发布门；这些在组装阶段接。

## 3. 数据契约（JSON 可序列化，字段名固定）

```json
Comment  = { "id": "bvid:rpid", "bvid": "...", "text": "...", "ctime": 1790000000,
             "like": 123, "zone": "...", "video_title": "..." }

Evidence = { "comment_id": "bvid:rpid", "quote": "原文子串",
             "ctime": 1790000000, "like": 123, "bvid": "..." }

Candidate = { "candidate_id": "sha1(canonical)", "surface": "原文子串",
              "canonical": "...", "type": "词|句式|谐音|缩写|名场面",
              "meaning": "...", "origin_quote": "...", "origin_ref": "...",
              "evidence": [Evidence, ...], "confidence": 0.0,
              "known": false, "source": "llm_extract|rule|seed",
              "comment_7d_count": 0, "comment_df_video": 0,
              "age_days": 0.0 }

Verdict  = { "candidate_id": "...", "verdict": "approve|reject|merge|unclear",
             "meme_prob": 0.0, "novelty": 0.0, "origin": "...",
             "reason": "...", "evidence_ids": ["..."], "merge_into": null }
```

硬规则：
- `surface` 必须是某条 evidence 的**原文子串**；不能编造。
- `approve` 至少要有 **2 条评论 + 2 个视频**的证据；不足只能 `unclear`。
- 纯 ASCII/数字、通用词、新闻/专名、无出处的一律 reject。
- 输出按 `canonical` 归一化去重；同族合并结果要可解释。

## 4. 对外接口（组装时 app 只依赖这些）

```python
collect_comments(targets: list[dict], client, *, window_days=30, max_per_video=40) -> list[Comment]

extract_candidates(comments: list[Comment], llm, *,
                   max_calls=3, max_comments=450, prompt_version="...") -> ExtractionResult
# ExtractionResult = { "candidates": [Candidate], "reason": str, "calls": int, ... }

review_candidates(candidates: list[Candidate], llm, *,
                  max_rounds=3, target=10, max_calls=24) -> ReviewResult
# ReviewResult = { "approved": [...], "rejected": [...], "merged": [...],
#                  "rounds": int, "calls": int, "fallback_used": bool, ... }

evaluate(predictions: list[Candidate], labels: list[dict]) -> dict
# precision@k / recall / evergreen_rate / evidence_rate / by-zone
```

要求：纯函数优先；LLM/HTTP 客户端由外部注入；失败返回 `reason`，不抛到调用方；JSON schema 校验；temperature 0。

## 5. CLI（给工程会话自己验收）

```bash
python -m tools.meme_info collect --videos ids.txt --out /tmp/comments.json
python -m tools.meme_info extract --comments /tmp/comments.json --out /tmp/candidates.json
python -m tools.meme_info review --candidates /tmp/candidates.json --out /tmp/verdicts.json
python -m tools.meme_info eval --pred /tmp/verdicts.json --labels labels.json
```

无 key 时 `extract/review` 明确输出 `not_configured` 并 exit 2；不伪造。

## 6. 验收标准

1. 单测覆盖：证据子串校验、注入文本、坏 JSON、预算/超时、去重、合并、2 评论/2 视频门槛。
2. 30–50 条人工标注（近期真梗/老梗/非梗）跑 `evaluate`，报告 precision@5/10、evidence_rate、evergreen_rate。
3. 真机 1–2 轮写 `/tmp`，输出 candidates/verdicts，附 10 条样本；不写生产库。
4. 成本可复现：每轮调用数、token、估算费用。
5. 交付 `meme_info/README.md` + `tools/` + tests + 一份 `REPORT.md`。

## 7. 组装契约（工程会话不用做，但按此设计）

- app 层只做 adapter：把 `Comment/Candidate/Verdict` 映射到现有 `pipeline`/`store`。
- 模块不感知 DB；组装时由 app 负责落库、发布门、API、看板。
- 模块版本化：`schema_version` + `prompt_version` 写进输出，便于回放。
- 目标架构：采集 worker → 规则引擎 → LLM 抽取/审核 worker → 发布门 → 读模型；SQLite 由 repository + 队列替代。
- 第一组装切面：**发布门 + 审核状态机**（当前 `run_llm_gate` 内嵌在 `pipeline`，质量结论未达标，先独立出来）。

## 8. 不要做的事

- 不要直接改 `app/pipeline.py` 来“顺手接上”；组装由 PM 统一做，避免再次耦合。
- 不要把 key、真实用户数据、生产 DB 写进仓库或报告。
- 不要用 fake LLM 的通过结果宣称质量达标；fake 只证明接线。
- 不要为凑 Top10 放宽证据门槛；宁缺毋滥。
