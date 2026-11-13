"""连接池行为测试。

池的正确性依赖三个不变量，各对应一个测试：
1. 同线程同库反复借出是同一个连接（收益来源）；
2. DB_PATH 换了必须换连接（测试隔离不串库）；
3. 上一个使用者忘 commit，借出时必须被回滚（复用连接最阴险的坑：
   不回滚的话，下一个使用者会读到别人的中间状态，且只在并发/复用时复现）。
"""
from pathlib import Path

from app.core import db


def test_same_thread_reuses_connection(tmp_path):
    db.close_thread_conns()
    c1 = db.get_conn()
    c2 = db.get_conn()
    assert c1 is c2, "同线程同库必须复用同一个连接"


def test_changing_db_path_switches_connection(tmp_path, monkeypatch):
    db.close_thread_conns()
    orig = db.DB_PATH
    try:
        c1 = db.get_conn()
        new_db = tmp_path / "other.sqlite"
        monkeypatch.setattr(db, "DB_PATH", str(new_db))
        c2 = db.get_conn()
        assert c1 is not c2, "库换了，连接必须换"
        assert c2 is db.get_conn()
    finally:
        db.DB_PATH = orig
        db.close_thread_conns()


def test_uncommitted_transaction_is_rolled_back_on_checkout(tmp_path):
    db.close_thread_conns()
    conn = db.get_conn()
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.execute("INSERT INTO t VALUES (42)")     # 故意不 commit
    assert conn.in_transaction
    again = db.get_conn()                          # 归还后再借出
    n = again.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert n == 0, "上一个使用者未提交的写入不能泄漏给下一个使用者"
