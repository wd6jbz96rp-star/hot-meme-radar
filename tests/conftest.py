"""测试统一关闭真实网络；临时报告由各测试自己指定。"""
import os

os.environ.setdefault("HOTMEME_OFFLINE", "1")
