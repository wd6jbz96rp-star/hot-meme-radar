"""测试统一使用临时库/离线，避免触网或写生产数据。"""
import os

os.environ.setdefault("HOTMEME_OFFLINE", "1")
os.environ.setdefault("HOTMEME_DB", "/tmp/hotmeme_pytest.db")
