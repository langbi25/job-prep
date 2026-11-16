"""SSRF 白名单测试（零真实网络，DNS 与 HTTP 全部替身）。

要钉的是"绕过路径"，不是"正常路径"——正常抓取不需要测，攻击者的花样才需要：
- 字面量私网 IP、十进制/十六进制变形、IPv6 回环与 IPv4-mapped；
- 内网域名后缀与云元数据主机名；
- 公网 URL 302 到内网（逐跳校验才挡得住）；
- 域名解析到多个 IP 时只要有一个是内网就拒（DNS 轮询绕过）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import netguard  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("INGEST_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.delenv("INGEST_ALLOWED_PORTS", raising=False)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x",
    "http://127.1/x",
    "http://0.0.0.0/x",
    "http://10.0.0.5/x",
    "http://172.16.3.4/x",
    "http://192.168.1.1/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.64.1.1/x",             # CGNAT，不在 is_private 里
    "http://[::1]/x",
    "http://[fe80::1]/x",
    "http://[::ffff:10.0.0.1]/x",      # IPv4-mapped IPv6
    "http://localhost:8080/x",
    "http://printer.local/x",
    "http://metadata.google.internal/computeMetadata/v1/",
    "file:///etc/passwd",
    "gopher://127.0.0.1:6379/_INFO",
    "http://user:pass@127.0.0.1/x",
])
def test_blocked_targets(url):
    with pytest.raises(netguard.UnsafeURLError):
        netguard.check_url(url)


def test_public_ip_allowed():
    assert netguard.check_url("https://1.1.1.1/x") == "https://1.1.1.1/x"
    assert netguard.check_url("http://93.184.216.34/") == "http://93.184.216.34/"


def test_port_whitelist():
    with pytest.raises(netguard.UnsafeURLError):
        netguard.check_url("http://1.1.1.1:6379/")
    # 常见站点端口放行
    assert netguard.check_url("http://1.1.1.1:80/").startswith("http://1.1.1.1")


def test_domain_resolving_to_private_is_blocked(monkeypatch):
    """真实攻击形态：域名本身无害，DNS 指向内网。"""
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34", "10.1.2.3"])
    with pytest.raises(netguard.UnsafeURLError) as e:
        netguard.check_url("https://evil.example.com/x")
    assert "10.1.2.3" in str(e.value)


def test_domain_with_only_public_ips_allowed(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34"])
    assert netguard.check_url("https://example.com/a?b=1") == "https://example.com/a?b=1"


def test_dns_failure_is_rejected(monkeypatch):
    def boom(host):
        raise OSError("no such host")
    monkeypatch.setattr(netguard, "_resolve", boom)
    with pytest.raises(netguard.UnsafeURLError):
        netguard.check_url("https://nope.invalid/")


class _Resp:
    def __init__(self, status, headers=None, body=b"", encoding="utf-8"):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.encoding = encoding
        self.text = body.decode(encoding, "replace")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self):
        yield self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """按 URL 返回预设响应，模拟重定向链。"""

    def __init__(self, routes):
        self.routes = routes
        self.seen = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url):
        self.seen.append(url)
        return self.routes[url]


def test_redirect_to_private_is_blocked(monkeypatch):
    """公网 302 → 内网：只要不逐跳校验就挡不住。"""
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34"])
    routes = {
        "https://public.example.com/a": _Resp(302, {"location": "http://169.254.169.254/"}),
    }
    monkeypatch.setattr(netguard.httpx, "Client", lambda **kw: _Client(routes))
    with pytest.raises(netguard.UnsafeURLError) as e:
        netguard.fetch_text("https://public.example.com/a")
    assert "169.254.169.254" in str(e.value)


def test_redirect_chain_within_limit(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34"])
    routes = {
        "https://a.example.com/1": _Resp(301, {"location": "https://a.example.com/2"}),
        "https://a.example.com/2": _Resp(200, body="<html>正文</html>".encode()),
    }
    monkeypatch.setattr(netguard.httpx, "Client", lambda **kw: _Client(routes))
    final, text = netguard.fetch_text("https://a.example.com/1")
    assert final == "https://a.example.com/2"
    assert "正文" in text


def test_too_many_redirects(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34"])
    routes = {
        f"https://a.example.com/{i}": _Resp(302, {"location": f"https://a.example.com/{i+1}"})
        for i in range(10)
    }
    monkeypatch.setattr(netguard.httpx, "Client", lambda **kw: _Client(routes))
    with pytest.raises(netguard.UnsafeURLError):
        netguard.fetch_text("https://a.example.com/0")


def test_oversize_body_aborted(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.216.34"])
    routes = {"https://a.example.com/big": _Resp(200, body=b"x" * 5000)}
    monkeypatch.setattr(netguard.httpx, "Client", lambda **kw: _Client(routes))
    with pytest.raises(netguard.UnsafeURLError):
        netguard.fetch_text("https://a.example.com/big", max_bytes=1000)


def test_escape_hatch_allows_private(monkeypatch):
    """本地开发要抓 127.0.0.1 的临时页面：显式开关，默认关闭。"""
    monkeypatch.setenv("INGEST_ALLOW_PRIVATE_URLS", "1")
    assert netguard.check_url("http://127.0.0.1:8765/page")
