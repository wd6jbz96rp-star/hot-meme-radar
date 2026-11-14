from __future__ import annotations

import time
from pathlib import Path

import pytest

from meme_info.models import Comment, Corpus, Doc, Video

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
NOW = int(time.mktime(time.strptime("2026-11-14 12:00:00", "%Y-%m-%d %H:%M:%S")))
DAY = 86400


@pytest.fixture
def now() -> int:
    return NOW


@pytest.fixture
def day() -> int:
    return DAY


def make_video(bvid: str, title: str, *, aid: int = 0, pubdate: int = 0,
               view: int = 0, like: int = 0, reply: int = 0, danmaku: int = 0,
               owner: str = "", source: str = "ranking:119", zone: str = "鬼畜") -> Video:
    return Video(
        bvid=bvid, aid=aid or abs(hash(bvid)) % 10_000_000, title=title,
        pubdate=pubdate, view=view, like=like, reply=reply, danmaku=danmaku,
        owner=owner, source=source, zone=zone,
    )


def make_comment(bvid: str, cid: str, text: str, *, ctime: int, like: int = 0,
                 title: str = "", zone: str = "鬼畜") -> Comment:
    return Comment(id=cid, bvid=bvid, text=text, ctime=ctime, like=like,
                   zone=zone, video_title=title)


def make_corpus(videos: list[Video], comments: list[Comment]) -> Corpus:
    docs = [
        Doc(id=f"{video.bvid}:title", text=video.title, kind="title",
            bvid=video.bvid, ctime=video.pubdate, zone=video.zone,
            video_title=video.title)
        for video in videos
    ]
    docs += [
        Doc(id=comment.id, text=comment.text, kind="comment", bvid=comment.bvid,
            ctime=comment.ctime, like=comment.like, zone=comment.zone,
            video_title=comment.video_title)
        for comment in comments
    ]
    return Corpus(
        videos=videos,
        comments=comments,
        docs=docs,
        stats={"videos": len(videos), "comments": len(comments), "docs": len(docs)},
        generated_at="",
    )
