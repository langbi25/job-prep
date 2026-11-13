"""鉴权 + 审计测试（app/core/guard.py）。

判据：
- 401 必须拦在 /api 之外的所有东西前面，但 /health、静态页要放行（healthcheck 和页面不能死）；
- 两种头（X-API-Token / Authorization: Bearer）都要认，脚本和浏览器两种生态；
- APP_TOKEN 为空 = 完全关闭，本地脚本不能因为这次改动坏掉；
- 审计只记写操作与 401，GET 不记；401 的 actor 是 anonymous。
"""
import pytest
from fastapi.testclient import TestClient

from app.core import config, db
from app.main import app


@pytest.fixture()
def guarded(tmp_path, monkeypatch):
    db.close_thread_conns()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    monkeypatch.setattr(config, "APP_TOKEN", "tok-test-123")
    yield TestClient(app)
    db.close_thread_conns()


@pytest.fixture()
def open_mode(tmp_path, monkeypatch):
    db.close_thread_conns()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    monkeypatch.setattr(config, "APP_TOKEN", "")
    yield TestClient(app)
    db.close_thread_conns()


def _audit_rows(client):
    conn = db.get_conn()
    return conn.execute("SELECT actor, action, path, status FROM audit_log ORDER BY id").fetchall()


def test_missing_token_401(guarded):
    r = guarded.get("/api/obs/backend")
    assert r.status_code == 401
    assert "令牌" in r.json()["detail"]


def test_wrong_token_401_and_audited(guarded):
    r = guarded.get("/api/obs/backend", headers={"X-API-Token": "wrong"})
    assert r.status_code == 401
    rows = _audit_rows(guarded)
    assert len(rows) == 1 and rows[0][0] == "anonymous" and rows[0][3] == 401


def test_valid_token_via_header(guarded):
    r = guarded.get("/api/obs/backend", headers={"X-API-Token": "tok-test-123"})
    assert r.status_code == 200


def test_valid_token_via_bearer(guarded):
    r = guarded.get("/api/obs/backend", headers={"Authorization": "Bearer tok-test-123"})
    assert r.status_code == 200


def test_health_exempt(guarded):
    assert guarded.get("/health").status_code == 200


def test_static_exempt(guarded):
    # 静态页不拦（dist 是否存在都可能，但绝不能 401）
    assert guarded.get("/").status_code != 401
    assert guarded.get("/docs").status_code != 401


def test_empty_token_disables_auth(open_mode):
    assert open_mode.get("/api/obs/backend").status_code == 200
    assert open_mode.get("/health").status_code == 200


def test_write_audited_even_404(guarded):
    # 写方法即使路由 404 也落审计（外层中间件先于路由匹配）；
    # 不带 token 会先 401（那也落审计，见上一个用例），带 token 才走到路由层。
    guarded.post("/api/nonexistent-path", json={}, headers={"X-API-Token": "tok-test-123"})
    rows = _audit_rows(guarded)
    assert len(rows) == 1 and rows[0][1] == "POST"
    assert rows[0][3] in (404, 405)   # 路由层拒绝（路径不存在 / 方法不匹配），审计照记


def test_get_not_audited(guarded):
    guarded.get("/api/obs/backend", headers={"X-API-Token": "tok-test-123"})
    assert _audit_rows(guarded) == []
