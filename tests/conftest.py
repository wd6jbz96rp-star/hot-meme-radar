"""测试统一关闭调度器与真实网络，避免测试触网写库。"""
import os

os.environ.setdefault("HOTMEME_OFFLINE", "1")
os.environ.setdefault("HOTMEME_DISABLE_SCHEDULER", "1")
os.environ.setdefault("HOTMEME_DB", "/tmp/hotmeme_pytest.db")
