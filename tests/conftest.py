"""全局测试夹具。

连接池让每个测试结束后都可能残留指向临时目录的数据库句柄——
Windows 上句柄没关，pytest 清理 tmp_path 时报 WinError 32。
在 autouse 里统一归还，10 个换 DB_PATH 的测试文件就不用各改各的 fixture。
"""
import pytest

from app.core import config, db
from app.observability import tracing as tr


@pytest.fixture(autouse=True)
def _release_db_conns(monkeypatch):
    # 评测进料默认在测试里关闭：失败轮会写 data/eval_intake.jsonl（真实数据目录），
    # 测试不该往里面倒垃圾。被测模块在自己的用例里显式打开。
    monkeypatch.setenv("EVAL_INTAKE", "0")
    # 测试必须对宿主 .env 免疫（CI 是干净检出，没有 .env）：llm._chat_json 入口
    # 会检查 config.LLM_API_KEY，本机跑测试靠 .env 兜着，CI 上 6 个用例因此团灭
    # （2026-11-18 首次暴露）。这里统一给假 key——真网络调用没有测试依赖它。
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key-hermetic", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "test-key-hermetic")
    yield
    db.close_thread_conns()
    # span 用独立连接写库（避开业务事务），不在 db 的池里，得单独关。
    # 漏了这一步，Windows 上临时目录会因句柄占用删不掉，表现为 PermissionError。
    tr.close_span_conn()
