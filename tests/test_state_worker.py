"""Phase G ``tools.state_worker`` 编排测试。

全部 mock / 内存：不真实跑 B 站、不需要 Postgres，也不触碰生产报告。
覆盖：单轮 B→C→D→E 顺序、报告原子写、degraded 备份 last_good、失败保留旧报告、
单写者锁重叠跳过、dry-run 事务回滚、审核默认关闭/开关、CLI/JSON、密码脱敏。
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

import pytest

from meme_info.models import PoolRoundResult, VideoRouteResult, WindowSample
from tools import state_worker

NOW = 1_700_000_000


# ---------------------------------------------------------------- fakes
def ok_route(round_id: int = 101, *, status: str = "ok") -> VideoRouteResult:
    result = VideoRouteResult(
        generated_at="2026-11-22T00:00:00+08:00",
        started_at=NOW - 60,
        window_start=NOW - 960,
        window_end=NOW,
        status=status,
        round_id=round_id,
    )
    result.stats = {
        "degraded_reason": "" if status == "ok" else "no candidates",
        "elapsed_seconds": 0.1,
    }
    return result


def ok_sample(round_id: int = 101, *, status: str = "ok") -> WindowSample:
    sample = WindowSample(
        generated_at="2026-11-22T00:00:00+08:00",
        started_at=NOW - 30,
        window_start=NOW - 960,
        window_end=NOW,
        status=status,
        round_id=round_id,
    )
    sample.stats = {
        "db_written": True,
        "degraded_reason": "" if status == "ok" else "comments=0<600",
        "elapsed_seconds": 0.1,
    }
    return sample


def ok_pool(round_id: int = 101, *, status: str = "ok") -> PoolRoundResult:
    result = PoolRoundResult(
        generated_at="2026-11-22T00:00:00+08:00",
        round_id=round_id,
        window_start=NOW - 960,
        window_end=NOW,
        status=status,
        degraded=status != "ok",
    )
    result.stats = {"db_written": True}
    return result


def v2_report(round_id: int = 101, *, status: str = "ok") -> dict:
    return {
        "schema_version": "meme-info/v2",
        "generated_at": "2026-11-22T00:00:00+08:00",
        "status": status,
        "round": {
            "id": round_id,
            "window_start": "2026-11-21T23:44:00+08:00",
            "window_end": "2026-11-21T23:59:00+08:00",
        },
        "counts": {
            "pool1": 3,
            "pool2": 1,
            "pool1_only": 2,
            "pool1_out": 0,
            "observations": 3,
        },
        "stats": {"degraded": status != "ok"},
    }


class FakeConn:
    def __init__(self) -> None:
        self.rolled_back = False
        self.closed = False

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_pipeline(monkeypatch):
    """把 B/C/D/E/F 五段替换成内存 fake，并记录调用顺序。"""
    state: dict = {
        "calls": [],
        "route": ok_route(),
        "sample": ok_sample(),
        "pool": ok_pool(),
        "report": v2_report(),
        "finalized": [],
        "audits": [],
        "conn": FakeConn(),
    }

    def fake_route(source=None, plan=None, **kwargs):
        state["calls"].append("video_route")
        return state["route"]

    def fake_window(source=None, plan=None, **kwargs):
        state["calls"].append("window_sample")
        state["sample_kwargs"] = kwargs
        return state["sample"]

    def fake_pool(sample, **kwargs):
        state["calls"].append("pool_round")
        state["pool_kwargs"] = kwargs
        return state["pool"]

    def fake_report(**kwargs):
        state["calls"].append("report_from_db")
        return state["report"]

    def fake_finalize(round_id, **kwargs):
        state["calls"].append("finalize")
        state["finalized"].append({"round_id": round_id, **kwargs})
        return {
            "round_id": round_id,
            "status": kwargs.get("status"),
            "degraded_reason": ";".join(kwargs.get("degraded_reasons") or []),
            "finished_at": "2026-11-22T00:00:00+08:00",
        }

    def fake_audit(**kwargs):
        state["calls"].append("audit")
        state["audits"].append(kwargs)
        return {"enabled": True, "queue": 2, "audited": 1, "skipped": 1, "errors": []}

    monkeypatch.setattr(state_worker, "run_video_route", fake_route)
    monkeypatch.setattr(state_worker, "run_window_sample", fake_window)
    monkeypatch.setattr(state_worker, "run_pool_round", fake_pool)
    monkeypatch.setattr(state_worker, "report_from_db", fake_report)
    monkeypatch.setattr(state_worker, "finalize_round_db", fake_finalize)
    monkeypatch.setattr(state_worker, "run_audit_round", fake_audit)
    monkeypatch.setattr(
        state_worker.state_mod,
        "database_url",
        lambda explicit=None: explicit or "postgresql://fake",
    )
    monkeypatch.setattr(
        state_worker.state_mod, "connect", lambda url=None, autocommit=False: state["conn"]
    )
    monkeypatch.delenv("MEME_INFO_VERIFY", raising=False)
    return state


def make_options(tmp_path: Path, **overrides) -> state_worker.RoundOptions:
    defaults = dict(
        use_db=True,
        report=tmp_path / "var" / "meme_info_latest.json",
        log=tmp_path / "var" / "state_worker.log",
        lock=tmp_path / "var" / "state_worker.lock",
        now=NOW,
        verify=False,
    )
    defaults.update(overrides)
    return state_worker.RoundOptions(**defaults)


def write_report_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 报告辅助
def test_last_good_path_naming(tmp_path: Path) -> None:
    target = tmp_path / "meme_info_latest.json"
    assert state_worker.last_good_path(target) == tmp_path / "meme_info_latest.last_good.json"


def test_redact_database_password() -> None:
    text = "connection failed: postgresql://hotmeme_worker:s3cret@127.0.0.1:5432/hotmeme"
    redacted = state_worker.redact(text)
    assert "s3cret" not in redacted
    assert "postgresql://hotmeme_worker:***@" in redacted


# ---------------------------------------------------------------- 正常轮
def test_db_round_runs_b_c_d_e_in_order(fake_pipeline, tmp_path: Path) -> None:
    summary = state_worker.run_round(make_options(tmp_path))

    assert summary["ok"] is True
    assert summary["status"] == "ok"
    assert summary["use_db"] is True
    assert fake_pipeline["calls"][:5] == [
        "video_route",
        "window_sample",
        "pool_round",
        "finalize",
        "report_from_db",
    ]
    # C 收到 B 的 round id + targets；D 收到 round id
    assert fake_pipeline["sample_kwargs"]["round_id"] == 101
    assert fake_pipeline["sample_kwargs"]["video_route"] is fake_pipeline["route"]
    assert fake_pipeline["pool_kwargs"]["round_id"] == 101
    assert fake_pipeline["finalized"][0]["status"] == "ok"

    report_path = tmp_path / "var" / "meme_info_latest.json"
    assert report_path.is_file()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "meme-info/v2"
    assert payload["status"] == "ok"
    assert summary["report"]["bytes"] > 0
    assert summary["report"]["last_good"] == str(
        tmp_path / "var" / "meme_info_latest.last_good.json"
    )
    assert Path(summary["report"]["last_good"]).is_file()
    log_text = (tmp_path / "var" / "state_worker.log").read_text(encoding="utf-8")
    assert "round begin" in log_text and "round end" in log_text


def test_degraded_round_still_writes_and_backs_up_last_good(
    fake_pipeline, tmp_path: Path
) -> None:
    report_path = tmp_path / "var" / "meme_info_latest.json"
    previous = v2_report(status="ok")
    write_report_file(report_path, previous)
    # 模拟一个「上一轮已写好」的 last_good 不存在场景，本轮 degraded 前应备份
    fake_pipeline["sample"] = ok_sample(status="degraded")
    fake_pipeline["pool"] = ok_pool(status="degraded")
    fake_pipeline["report"] = v2_report(status="degraded")

    summary = state_worker.run_round(make_options(tmp_path))

    assert summary["ok"] is True
    assert summary["status"] == "degraded"
    assert summary["round_status"] == "degraded"
    assert any("phase_c" in reason for reason in summary["degraded_reasons"])
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["status"] == "degraded"
    last_good = json.loads(
        (tmp_path / "var" / "meme_info_latest.last_good.json").read_text(encoding="utf-8")
    )
    assert last_good == previous


def test_degraded_round_keeps_existing_last_good(fake_pipeline, tmp_path: Path) -> None:
    report_path = tmp_path / "var" / "meme_info_latest.json"
    last_good_path = tmp_path / "var" / "meme_info_latest.last_good.json"
    write_report_file(report_path, v2_report(status="degraded"))
    write_report_file(last_good_path, {"status": "ok", "marker": "keep-me"})
    fake_pipeline["sample"] = ok_sample(status="degraded")
    fake_pipeline["pool"] = ok_pool(status="degraded")
    fake_pipeline["report"] = v2_report(status="degraded")

    state_worker.run_round(make_options(tmp_path))

    kept = json.loads(last_good_path.read_text(encoding="utf-8"))
    assert kept["marker"] == "keep-me"


def test_report_status_forced_degraded_when_round_degraded(
    fake_pipeline, tmp_path: Path
) -> None:
    """E 段报告若漏标 degraded（DB 状态没带上），编排器必须强制降级。"""
    fake_pipeline["sample"] = ok_sample(status="degraded")
    fake_pipeline["pool"] = ok_pool(status="degraded")
    fake_pipeline["report"] = v2_report(status="ok")

    summary = state_worker.run_round(make_options(tmp_path))

    payload = json.loads(
        (tmp_path / "var" / "meme_info_latest.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "degraded"
    assert any("不一致" in warning for warning in summary["warnings"])


# ---------------------------------------------------------------- 失败保护
def test_phase_b_failed_does_not_touch_old_report(fake_pipeline, tmp_path: Path) -> None:
    report_path = tmp_path / "var" / "meme_info_latest.json"
    write_report_file(report_path, {"status": "ok", "old": True})
    fake_pipeline["route"] = ok_route(status="failed")

    summary = state_worker.run_round(make_options(tmp_path))

    assert summary["ok"] is False
    assert summary["status"] == "failed"
    assert summary["errors"]
    assert json.loads(report_path.read_text(encoding="utf-8")) == {"status": "ok", "old": True}
    assert not (tmp_path / "var" / "meme_info_latest.last_good.json").exists()
    assert fake_pipeline["finalized"][0]["status"] == "failed"


def test_stage_exception_keeps_old_report_and_returns_exit_code(
    fake_pipeline, monkeypatch, tmp_path: Path
) -> None:
    report_path = tmp_path / "var" / "meme_info_latest.json"
    write_report_file(report_path, {"status": "ok", "old": True})

    def boom(*args, **kwargs):
        raise RuntimeError("bili down: postgresql://user:topsecret@host/db")

    monkeypatch.setattr(state_worker, "run_video_route", boom)
    exits: list[int] = []
    exit_code = state_worker.main(
        [
            "--once",
            "--json",
            "--output",
            str(report_path),
            "--log",
            str(tmp_path / "var" / "state_worker.log"),
            "--lock",
            str(tmp_path / "var" / "state_worker.lock"),
            "--database-url",
            "postgresql://fake",
        ]
    )
    exits.append(exit_code)

    assert exits == [state_worker.EXIT_FAIL]
    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "status": "ok",
        "old": True,
    }
    log_text = (tmp_path / "var" / "state_worker.log").read_text(encoding="utf-8")
    assert "topsecret" not in log_text
    assert "postgresql://user:***@" in log_text


def test_report_write_failure_preserves_old_report(
    fake_pipeline, monkeypatch, tmp_path: Path
) -> None:
    report_path = tmp_path / "var" / "meme_info_latest.json"
    write_report_file(report_path, {"status": "ok", "old": True})

    def broken_write(report, path):
        raise OSError("disk full")

    monkeypatch.setattr(state_worker, "write_report", broken_write)
    summary = state_worker.run_round(make_options(tmp_path))

    assert summary["ok"] is False
    assert summary["status"] == "failed"
    assert any("报告写入失败" in error["message"] for error in summary["errors"])
    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "status": "ok",
        "old": True,
    }


# ---------------------------------------------------------------- 单写者锁
def test_second_instance_skips_when_lock_held(fake_pipeline, tmp_path: Path) -> None:
    lock_file = tmp_path / "var" / "state_worker.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_file, "a+", encoding="utf-8")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        summary = state_worker.run_round(make_options(tmp_path))
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    assert summary["ok"] is True
    assert summary["status"] == "skipped_overlap"
    assert fake_pipeline["calls"] == []
    assert not (tmp_path / "var" / "meme_info_latest.json").exists()
    assert "skipped_overlap" in (tmp_path / "var" / "state_worker.log").read_text(
        encoding="utf-8"
    )


def test_lock_released_after_round(fake_pipeline, tmp_path: Path) -> None:
    state_worker.run_round(make_options(tmp_path))
    lock_file = tmp_path / "var" / "state_worker.lock"
    with open(lock_file, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # 不应抛错
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- dry-run / 内存模式
def test_dry_run_rolls_back_and_skips_report(fake_pipeline, tmp_path: Path) -> None:
    summary = state_worker.run_round(make_options(tmp_path, dry_run=True))

    conn = fake_pipeline["conn"]
    assert conn.rolled_back is True
    assert conn.closed is True
    assert summary["ok"] is True
    assert summary["dry_run"] is True
    assert summary["stages"]["report_write"]["skipped"] is True
    assert not (tmp_path / "var" / "meme_info_latest.json").exists()
    assert "audit" not in fake_pipeline["calls"]


def test_dry_run_audit_not_called_even_when_verify(fake_pipeline, tmp_path: Path) -> None:
    state_worker.run_round(make_options(tmp_path, dry_run=True, verify=True))
    assert "audit" not in fake_pipeline["calls"]


def test_memory_round_writes_explicit_output(fake_pipeline, tmp_path: Path) -> None:
    report_path = tmp_path / "out" / "report.json"
    summary = state_worker.run_round(
        make_options(
            tmp_path,
            use_db=False,
            report=report_path,
            extra={"explicit_output": True},
        )
    )

    assert summary["ok"] is True
    assert summary["use_db"] is False
    assert "report_from_db" not in fake_pipeline["calls"]
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "meme-info/v2"
    assert payload["status"] == "ok"


def test_memory_round_without_output_does_not_write(fake_pipeline, tmp_path: Path) -> None:
    report_path = tmp_path / "out" / "report.json"
    summary = state_worker.run_round(
        make_options(tmp_path, use_db=False, report=report_path)
    )
    assert summary["ok"] is True
    assert not report_path.exists()
    assert summary["stages"]["report_write"]["reason"] == "no_db_without_output"


# ---------------------------------------------------------------- 审核开关
def test_audit_disabled_by_default(fake_pipeline, tmp_path: Path) -> None:
    state_worker.run_round(make_options(tmp_path, verify=False))
    assert "audit" not in fake_pipeline["calls"]


def test_audit_runs_once_when_verify_enabled(fake_pipeline, tmp_path: Path) -> None:
    summary = state_worker.run_round(make_options(tmp_path, verify=True))
    assert fake_pipeline["calls"].count("audit") == 1
    assert summary["stages"]["audit"]["enabled"] is True


def test_audit_env_switch(monkeypatch, fake_pipeline, tmp_path: Path) -> None:
    monkeypatch.setenv("MEME_INFO_VERIFY", "1")
    options = make_options(tmp_path)
    options.verify = None
    options.__post_init__()
    state_worker.run_round(options)
    assert "audit" in fake_pipeline["calls"]


def test_audit_failure_does_not_fail_round(
    fake_pipeline, monkeypatch, tmp_path: Path
) -> None:
    def broken_audit(**kwargs):
        raise RuntimeError("llm key missing")

    monkeypatch.setattr(state_worker, "run_audit_round", broken_audit)
    summary = state_worker.run_round(make_options(tmp_path, verify=True))
    assert summary["ok"] is True
    assert any("phase_f" in warning for warning in summary["warnings"])


# ---------------------------------------------------------------- CLI
def test_cli_flags_parse() -> None:
    parser = state_worker.build_parser()
    args = parser.parse_args(["--once", "--dry-run", "--no-db", "--json", "--limit", "5"])
    options = state_worker.options_from_args(args)
    assert options.use_db is False
    assert options.dry_run is True
    assert options.json_output is True
    assert options.limit == 5


def test_cli_json_stdout_is_single_json_object(
    fake_pipeline, tmp_path: Path, capsys
) -> None:
    report_path = tmp_path / "out" / "report.json"
    exit_code = state_worker.main(
        [
            "--no-db",
            "--json",
            "--output",
            str(report_path),
            "--log",
            str(tmp_path / "var" / "state_worker.log"),
            "--lock",
            str(tmp_path / "var" / "state_worker.lock"),
            "--now",
            str(NOW),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "ok"
    assert exit_code == state_worker.EXIT_OK
    assert report_path.is_file()


def test_report_path_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOTMEME_MEME_INFO_REPORT", str(tmp_path / "a.json"))
    assert state_worker.report_path() == tmp_path / "a.json"
    monkeypatch.delenv("HOTMEME_MEME_INFO_REPORT")
    monkeypatch.setenv("MEME_INFO_REPORT", str(tmp_path / "b.json"))
    assert state_worker.report_path() == tmp_path / "b.json"


def test_options_from_args_rejects_both_output_and_report(tmp_path: Path) -> None:
    parser = state_worker.build_parser()
    args = parser.parse_args(
        ["--output", str(tmp_path / "a.json"), "--report", str(tmp_path / "b.json")]
    )
    with pytest.raises(SystemExit):
        state_worker.options_from_args(args)
