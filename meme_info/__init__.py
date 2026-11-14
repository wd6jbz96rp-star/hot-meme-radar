"""热梗信息模块（独立库）。

公开接口：
- ``collect_comments`` / ``collect_corpus``：采集评论与多通道语料
- ``extract_candidates`` / ``merge_families``：候选发现与梗族归并
- ``HeatModel``：可解释热度打分
- ``find_hot_memes`` / ``query_from_corpus``：端到端查询
- ``evaluate``：精度/召回/校准评测
"""

from .bili import BiliSource
from .collect import CollectionPlan, collect_comments, collect_corpus
from .discover import DiscoveryConfig, extract_candidates, extract_meta_mentions, merge_families
from .evaluate import evaluate, evaluate_discovery
from .heat import HeatConfig, HeatModel
from .knowledge import Knowledge
from .models import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    Candidate,
    Comment,
    Corpus,
    Danmaku,
    Doc,
    Evidence,
    HotMemeReport,
    Meme,
    MetaMention,
    Usage,
    Video,
)
from .query import QueryConfig, find_hot_memes, query_from_corpus
from .verify import ChatClient, SemanticVerifier, extract_candidates as llm_extract_candidates
from .verify import review_candidates

__version__ = "1.0.0"
