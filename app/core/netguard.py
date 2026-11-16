"""出站 URL 白名单（SSRF 防护）：只允许公网 http(s)，逐跳校验重定向。

为什么要做：`ingest` 的通道 B 会抓用户给的任意 URL。没有校验时，
`http://127.0.0.1:8765/api/...`、`http://169.254.169.254/`（云元数据）、
`http://10.0.0.5/` 都能让**服务器替用户去访问内网**——这就是 SSRF。
个人内网危害有限，放到平台上就是高危漏洞。

四道闸：
1. 协议白名单：只有 http/https（`file://`、`gopher://`、`dict://` 一律拒）；
2. 端口白名单：默认 80/443（可用 `INGEST_ALLOWED_PORTS` 扩）；
3. **解析后**逐个 IP 校验：私网/回环/链路本地/保留/组播/未指定全拒——
   只看主机名字符串会被 `127.0.0.1.nip.io`、`0x7f000001` 这类绕过；
4. **逐跳校验重定向**：每跳都重新过 1~3，防止"公网 302 → 内网"。

已知残余风险（诚实写清）：DNS rebinding——校验时解析到公网 IP，真正连接前
DNS 又改成内网。彻底的修法是把连接钉在已校验的 IP 上（Host 头 + TLS SNI），
httpx 做这件事要自定义 transport。当前用"校验后立刻连接 + 短超时 + 限跳数"
缩小窗口，并把它记在这里而不是假装没有。
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urljoin, urlsplit

import httpx

DEFAULT_PORTS = {80, 443}
MAX_REDIRECTS = 3
MAX_BYTES = 2_000_000          # 2MB：面经正文远小于这个数，防"下载大文件打满内存"
BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")
BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata.goog"}


class UnsafeURLError(ValueError):
    """URL 被白名单拒绝。

    继承 ValueError 是有意的：API 层已经把 ValueError 映射成 400，
    这类拒绝属于"请求不合法"，不是服务器故障。
    """


def _allowed_ports() -> set[int]:
    raw = (os.environ.get("INGEST_ALLOWED_PORTS") or "").strip()
    if not raw:
        return set(DEFAULT_PORTS)
    out = set()
    for part in raw.split(","):
        try:
            out.add(int(part.strip()))
        except ValueError:
            continue
    return out or set(DEFAULT_PORTS)


def _allow_private() -> bool:
    # 逃生开关：本地开发/测试要抓 127.0.0.1 的临时页面时用；默认关闭
    return (os.environ.get("INGEST_ALLOW_PRIVATE_URLS") or "").strip() in ("1", "true", "yes")


def _resolve(host: str) -> list[str]:
    """解析主机名到全部 IP。测试通过 monkeypatch 替换它，避免真实 DNS。"""
    infos = socket.getaddrinfo(host, None)
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


def _ip_blocked(ip: str) -> str | None:
    """返回拒绝理由（None = 允许）。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "无法解析的地址"
    # IPv4-mapped IPv6（::ffff:10.0.0.1）必须拆开判断，否则绕过私网检查
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_loopback:
        return "回环地址"
    if addr.is_private:
        return "内网地址"
    if addr.is_link_local:
        return "链路本地地址（含云元数据 169.254.169.254）"
    if addr.is_multicast:
        return "组播地址"
    if addr.is_reserved or addr.is_unspecified:
        return "保留地址"
    # 100.64.0.0/10（运营商级 NAT）不在 is_private 里，单独拦
    if addr.version == 4 and ipaddress.ip_network("100.64.0.0/10").supernet_of(
            ipaddress.ip_network(f"{addr}/32", strict=False)):
        return "共享地址段（CGNAT）"
    return None


def check_url(url: str) -> str:
    """校验并返回规范化后的 URL（scheme + host 小写）；不合规抛 UnsafeURLError。"""
    raw = (url or "").strip()
    if not raw:
        raise UnsafeURLError("URL 为空")
    p = urlsplit(raw)
    scheme = (p.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UnsafeURLError(f"只允许 http/https，收到 {scheme or '空'} 协议")
    host = (p.hostname or "").strip().lower()
    if not host:
        raise UnsafeURLError("URL 缺少主机名")
    try:
        port = p.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise UnsafeURLError("URL 端口不合法")
    if port not in _allowed_ports():
        # 逃生开关连端口一起放开：本地 dev server 就在 8765，只放开 IP 不放开端口
        # 等于开关没用。生产不开这个开关。
        if not _allow_private():
            raise UnsafeURLError(f"端口 {port} 不在白名单 {sorted(_allowed_ports())}")

    if not _allow_private():
        if host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
            raise UnsafeURLError(f"主机 {host} 属于内网/元数据服务")
        # 字面量 IP 直接判，不走 DNS
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = None
        if literal is not None:
            why = _ip_blocked(str(literal))
            if why:
                raise UnsafeURLError(f"目标 IP {host} 被拒绝：{why}")
        else:
            try:
                ips = _resolve(host)
            except OSError as e:
                raise UnsafeURLError(f"域名解析失败：{host}（{e}）")
            if not ips:
                raise UnsafeURLError(f"域名 {host} 没有可用地址")
            for ip in ips:
                why = _ip_blocked(ip)
                if why:
                    raise UnsafeURLError(f"域名 {host} 解析到 {ip}，被拒绝：{why}")
    return raw


def fetch_text(url: str, *, timeout: float = 10.0, max_bytes: int = MAX_BYTES) -> tuple[str, str]:
    """带校验的抓取。返回 (最终 URL, 文本)。重定向逐跳校验。"""
    cur = check_url(url)
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
    }
    with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as client:
        for _ in range(MAX_REDIRECTS + 1):
            with client.stream("GET", cur) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location") or ""
                    if not loc:
                        raise UnsafeURLError(f"重定向缺少 Location（{resp.status_code}）")
                    cur = check_url(urljoin(cur, loc))   # 每一跳都重新校验
                    continue
                resp.raise_for_status()
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise UnsafeURLError(f"响应体超过上限 {max_bytes} 字节，已中止")
                    chunks.append(chunk)
                return cur, b"".join(chunks).decode(resp.encoding or "utf-8", "replace")
    raise UnsafeURLError(f"重定向超过 {MAX_REDIRECTS} 跳")
