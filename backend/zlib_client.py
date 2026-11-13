# -*- coding: utf-8 -*-
"""Z-Library 客户端：zlib.ch 入口跳转、DoH、挑战、EAPI/HTML 搜索、下载。

本文件是唯一接触第三方站点协议的地方；站点改版时优先改这里。协议依据是对
zlib.ch / z101.by 的实测，以及社区 EAPI 文档（heartleo/zlib）的可观察行为，
未复制其代码。

入口约定
--------
- 工具只配置入口 ``entry_url``（默认 https://zlib.ch/），不写死最终域；
- DoorwaySession 手动跟随 3xx，逐跳处理 JS 挑战，最终 origin 缓存 10 分钟；
- EAPI 请求不需要过挑战；HTML 的 /dl/ 路由需要挑战 cookie。

DNS
---
zlib.ch 在部分网络会被系统 DNS 解析成 0.0.0.0。DnsResolver 在系统 DNS 失败或
返回私网/未指定地址时走 DoH；DohHttpAdapter 连接 DoH 返回的 IP，但 SNI/Host
仍是原域名，TLS 证书按原域名校验。
"""
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from urllib.parse import quote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.poolmanager import PoolManager
from urllib3.util.connection import create_connection

DEFAULT_ENTRY = "https://zlib.ch/"
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
}
CHALLENGE_MARKERS = ("checking your browser", "a0_0x2a54", "c_token=",
                     "cookies are required to access this content")
CHALLENGE_MAX_ITERATIONS = 2_000_000
FINAL_ORIGIN_TTL = 10 * 60
_MAX_HOPS = 10
_CHUNK_SIZE = 64 * 1024
_DOWNLOAD_EXTENSIONS = {"epub", "pdf", "mobi", "azw3", "fb2", "djvu", "cbz", "cbr", "txt"}
_EBOOK_TYPES = ("application/epub+zip", "application/pdf", "application/x-mobipocket-ebook",
                "application/vnd.amazon.ebook", "application/zip",
                "application/octet-stream", "application/x-rar-compressed")


class ZlibError(Exception):
    """带错误码的客户端异常；code 会原样回给前端查文案。"""

    def __init__(self, code, message="", detail=""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.detail = detail or ""


def _json_payload(response):
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _text(value):
    if value is None:
        return ""
    return str(value)


def _int_or_none(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _ip_is_public(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if ip.version == 6 and getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return bool(ip.is_global and not ip.is_multicast)


def _ip_is_unspecified(value):
    try:
        return ipaddress.ip_address(value).is_unspecified
    except ValueError:
        return False


def _find_zlib_error(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, ZlibError):
            return exc
        seen.add(id(exc))
        exc = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    return None


def _safe_url(url):
    try:
        parts = urlsplit(url)
        path = parts.path or "/"
        return "%s://%s%s" % (parts.scheme, parts.netloc, path)
    except ValueError:
        return "<invalid url>"


def _origin(url):
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise ZlibError("entry.unreachable", "bad url: %s" % _safe_url(url))
    return "%s://%s" % (parts.scheme, parts.netloc)


def _validate_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ZlibError("entry.unreachable", "HTTPS required")
    if parts.username or parts.password:
        raise ZlibError("entry.unreachable", "credentials in url")
    host = parts.hostname
    if not host:
        raise ZlibError("entry.unreachable", "missing host")
    if _is_ip(host) and not _ip_is_public(host):
        raise ZlibError("dns.private", host)


def _is_challenge(response):
    text = (response.text or "")[:60000].lower()
    return any(marker in text for marker in CHALLENGE_MARKERS)


def _solve_challenge(session, response, max_iterations=CHALLENGE_MAX_ITERATIONS):
    """通过 zlib.ch 跳转链上的 SHA1 工作量证明，返回解出的 i。"""
    text = response.text or ""
    match = re.search(r"a0_0x2a54\s*=\s*\['([0-9A-Fa-f]{40})'", text)
    if not match:
        match = re.search(r"'([0-9A-Fa-f]{40})'", text)
    if not match:
        raise ZlibError("challenge.unsupported", "token not found")
    token = match.group(1)
    try:
        index = int(token[0], 16)
    except ValueError:
        raise ZlibError("challenge.unsupported", "bad token index")
    for i in range(max_iterations):
        digest = hashlib.sha1((token + str(i)).encode("ascii")).digest()
        if digest[index] == 0xB0 and digest[index + 1] == 0x0B:
            host = urlsplit(response.url).hostname or ""
            session.cookies.set("c_token", token + str(i), domain=host, path="/")
            session.cookies.set("c_time", "0.1", domain=host, path="/")
            return i
    raise ZlibError("challenge.unsupported", "too many iterations")


class DnsResolver(object):
    """系统 DNS 优先；被污染/不可用时走 DoH。返回值 None 表示交给默认解析。"""

    DOH_PROVIDERS = (
        "https://dns.alidns.com/resolve",
        "https://cloudflare-dns.com/dns-query",
    )

    def __init__(self, mode="auto", timeout=25, cache_ttl=300):
        self.mode = mode if mode in ("auto", "system", "doh") else "auto"
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache = {}
        self._lock = threading.Lock()
        self._doh_lock = threading.Lock()
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.headers.update({"User-Agent": BROWSER_HEADERS["User-Agent"],
                                      "Accept": "application/dns-json"})

    def resolve(self, host):
        host = (host or "").strip().rstrip(".")
        if not host:
            raise ZlibError("dns.unreachable", "empty host")
        if _is_ip(host):
            if not _ip_is_public(host):
                raise ZlibError("dns.private", host)
            return host
        cached = self._cache_get(host)
        if cached is not None:
            return cached or None
        if self.mode == "doh":
            return self._resolve_doh(host)
        system_ips = self._system_ips(host)
        public = [ip for ip in system_ips if _ip_is_public(ip)]
        if public:
            self._cache_set(host, "")
            return None
        # 0.0.0.0 / :: 是 DNS 屏蔽的常见回显，不是私网目标：继续走 DoH。
        if system_ips and not all(_ip_is_unspecified(ip) for ip in system_ips):
            raise ZlibError("dns.private", host)
        if self.mode == "system":
            raise ZlibError("dns.unreachable", host)
        return self._resolve_doh(host)

    def _system_ips(self, host):
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError:
            return []
        result = []
        for info in infos:
            try:
                ip = info[4][0]
            except (IndexError, TypeError):
                continue
            if ip and ip not in result:
                result.append(ip)
        return result

    def _resolve_doh(self, host):
        last_error = None
        with self._doh_lock:
            for provider in self.DOH_PROVIDERS:
                try:
                    resp = self._session.get(
                        provider, params={"name": host, "type": "A"},
                        headers={"Accept": "application/dns-json"},
                        timeout=min(self.timeout, 8))
                    if resp.status_code != 200:
                        last_error = "HTTP %d" % resp.status_code
                        continue
                    payload = _json_payload(resp)
                    answers = payload.get("Answer") or []
                    ips = []
                    if isinstance(answers, list):
                        for answer in answers:
                            if not isinstance(answer, dict) or answer.get("type") not in (1, None):
                                continue
                            value = _text(answer.get("data")).strip()
                            if value and value not in ips:
                                ips.append(value)
                    good = [ip for ip in ips if _ip_is_public(ip)]
                    if good:
                        self._cache_set(host, good[0])
                        return good[0]
                    last_error = "no public A record"
                except requests.RequestException as err:
                    last_error = str(err)
                    continue
        raise ZlibError("dns.unreachable", host, last_error or "")

    def _cache_get(self, host):
        with self._lock:
            item = self._cache.get(host)
            if not item:
                return None
            value, expires = item
            if time.time() >= expires:
                self._cache.pop(host, None)
                return None
            return value

    def _cache_set(self, host, value):
        with self._lock:
            self._cache[host] = (value, time.time() + self.cache_ttl)


class _DohHTTPSConnection(HTTPSConnection):
    doh_resolver = None

    def _new_conn(self):
        resolver = getattr(type(self), "doh_resolver", None)
        if resolver is not None:
            ip = resolver.resolve(self.host)
            if ip:
                return create_connection(
                    (ip, self.port), self.timeout,
                    source_address=self.source_address,
                    socket_options=self.socket_options)
        return super(_DohHTTPSConnection, self)._new_conn()


class _DohPoolManager(PoolManager):
    def __init__(self, resolver, **kwargs):
        self._doh_resolver = resolver
        super(_DohPoolManager, self).__init__(**kwargs)
        self.pool_classes_by_scheme = dict(self.pool_classes_by_scheme)
        conn_cls = type("_DohHTTPSConnection", (_DohHTTPSConnection,),
                        {"doh_resolver": resolver})
        pool_cls = type("_DohHTTPSConnectionPool", (HTTPSConnectionPool,),
                        {"ConnectionCls": conn_cls})
        self.pool_classes_by_scheme["https"] = pool_cls


class DohHttpAdapter(HTTPAdapter):
    """让 requests 在系统 DNS 不可用时连接 DoH 解析出的 IP，同时保留 SNI。"""

    def __init__(self, resolver, **kwargs):
        self.doh_resolver = resolver
        super(DohHttpAdapter, self).__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        self._pool_connections = connections
        self._pool_maxsize = maxsize
        self._pool_block = block
        self.poolmanager = _DohPoolManager(
            self.doh_resolver, num_pools=connections, maxsize=maxsize,
            block=block, **pool_kwargs)


class DoorwaySession(object):
    """从 zlib.ch 跟随跳转，处理挑战，产出最终 origin。"""

    def __init__(self, entry_url=DEFAULT_ENTRY, timeout=30, dns_mode="auto"):
        self.entry_url = _normalize_entry(entry_url)
        self.timeout = timeout
        self.resolver = DnsResolver(mode=dns_mode, timeout=timeout)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(BROWSER_HEADERS)
        self.session.mount("https://", DohHttpAdapter(self.resolver))
        self.final_origin = ""
        self.final_at = 0.0
        self.hops = []
        self._lock = threading.Lock()
        self.session_lock = threading.RLock()

    def final(self, force=False):
        with self._lock:
            if (not force and self.final_origin
                    and time.time() - self.final_at < FINAL_ORIGIN_TTL):
                return self.final_origin
            return self._resolve()

    def _request(self, url):
        try:
            return self.session.get(url, allow_redirects=False, timeout=self.timeout)
        except ZlibError:
            raise
        except requests.RequestException as err:
            zerr = _find_zlib_error(err)
            if zerr is not None:
                raise zerr
            raise ZlibError("entry.unreachable", str(err))

    def _resolve(self):
        url = self.entry_url
        hops = []
        challenge_attempts = 0
        with self.session_lock:
            for _ in range(_MAX_HOPS):
                resp = self._request(url)
                hops.append({"url": _safe_url(resp.url), "status": resp.status_code})
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise ZlibError("entry.http", "redirect without Location")
                    target = urljoin(resp.url, location)
                    _validate_url(target)
                    url = target
                    continue
                if resp.status_code in (403, 503) and _is_challenge(resp):
                    challenge_attempts += 1
                    if challenge_attempts > 5:
                        raise ZlibError("challenge.unsupported", "too many attempts")
                    solved = _solve_challenge(self.session, resp)
                    hops[-1]["challenge"] = solved
                    continue
                if resp.status_code == 200 and _is_challenge(resp):
                    challenge_attempts += 1
                    if challenge_attempts > 5:
                        raise ZlibError("challenge.unsupported", "too many attempts")
                    solved = _solve_challenge(self.session, resp)
                    hops[-1]["challenge"] = solved
                    continue
                if resp.status_code >= 400:
                    raise ZlibError("entry.http", "HTTP %d at %s" % (
                        resp.status_code, _safe_url(resp.url)))
                origin = _origin(resp.url)
                _validate_url(origin + "/")
                self.final_origin = origin
                self.final_at = time.time()
                self.hops = hops
                return origin
        raise ZlibError("entry.too_many_redirects", "> %d" % _MAX_HOPS)


def _normalize_entry(value):
    value = (value or DEFAULT_ENTRY).strip()
    if not value:
        value = DEFAULT_ENTRY
    if value.startswith("http://"):
        value = "https://" + value[7:]
    elif not value.startswith("https://"):
        value = "https://" + value
    parts = urlsplit(value)
    if not parts.netloc:
        raise ZlibError("params.invalid", "bad entry_url")
    _validate_url(value)
    return value if value.endswith("/") else value + "/"


class ZlibClient(object):
    """一个工具实例共享的 Z-Library 客户端。"""

    def __init__(self, entry_url=DEFAULT_ENTRY, dns_mode="auto", timeout=30):
        self.timeout = timeout
        self.doorway = DoorwaySession(entry_url, timeout, dns_mode)
        self._eapi = None
        self._lock = threading.Lock()
        self._request_lock = self.doorway.session_lock

    def _new_download_session(self):
        """每个下载任务独立 Session，共享 Session 在并发 get 时并不线程安全。"""
        session = requests.Session()
        session.trust_env = False
        session.headers.update({
            "User-Agent": BROWSER_HEADERS["User-Agent"],
            "Accept": "*/*",
        })
        session.mount("https://", DohHttpAdapter(self.doorway.resolver))
        return session

    def origin(self, force=False):
        return self.doorway.final(force=force)

    # ---------------------------------------------------------------- 请求

    def _request(self, method, url, *, data=None, headers=None, cookies=None,
                 timeout=None, allow_redirects=True):
        merged = {"Accept": "application/json, text/plain, */*"}
        if headers:
            merged.update(headers)
        try:
            with self._request_lock:
                return self.doorway.session.request(
                    method, url, data=data, headers=merged, cookies=cookies,
                    timeout=timeout or self.timeout, allow_redirects=allow_redirects)
        except ZlibError:
            raise
        except requests.RequestException as err:
            zerr = _find_zlib_error(err)
            if zerr is not None:
                raise zerr
            raise ZlibError("entry.unreachable", str(err))

    def _request_challenge_aware(self, method, url, *, data=None, headers=None,
                                 cookies=None, timeout=None, max_attempts=4):
        with self._request_lock:
            response = self._request(method, url, data=data, headers=headers,
                                     cookies=cookies, timeout=timeout)
            attempts = 0
            while _is_challenge(response) and attempts < max_attempts:
                _solve_challenge(self.doorway.session, response)
                response = self._request(method, url, data=data, headers=headers,
                                         cookies=cookies, timeout=timeout)
                attempts += 1
            if _is_challenge(response):
                raise ZlibError("challenge.unsupported", "challenge did not pass")
            return response

    # ---------------------------------------------------------------- EAPI

    def _eapi_headers(self, account=None, post=False):
        headers = {"Accept": "application/json"}
        if post:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if account:
            user_id = _text(account.get("remix_userid"))
            user_key = _text(account.get("remix_userkey"))
            if user_id:
                headers["remix-userid"] = user_id
            if user_key:
                headers["remix-userkey"] = user_key
        return headers

    def _account_cookies(self, account):
        cookies = dict((account or {}).get("cookies") or {})
        if account:
            if account.get("remix_userid"):
                cookies["remix_userid"] = _text(account["remix_userid"])
            if account.get("remix_userkey"):
                cookies["remix_userkey"] = _text(account["remix_userkey"])
            if account.get("bsrv"):
                cookies["bsrv"] = _text(account["bsrv"])
        cookies.pop("c_token", None)
        cookies.pop("c_time", None)
        return cookies

    def _eapi_json(self, method, origin, path, *, data=None, account=None,
                   timeout=None):
        response = self._request(
            method, origin + path, data=data,
            headers=self._eapi_headers(account, post=(method == "POST")),
            cookies=self._account_cookies(account) if account else None,
            timeout=timeout)
        payload = _json_payload(response)
        if not payload:
            if response.status_code in (403, 503):
                raise ZlibError("eapi.http", "HTTP %d" % response.status_code)
            raise ZlibError("eapi.http", "invalid JSON (HTTP %d)" % response.status_code)
        if payload.get("success") != 1:
            raise _eapi_error(payload.get("error") or "EAPI rejected")
        return payload

    def probe_eapi(self, origin, force=False):
        with self._lock:
            if self._eapi is not None and not force:
                return self._eapi
            try:
                self._eapi_json("GET", origin, "/eapi/info/domains", timeout=min(self.timeout, 20))
                self._eapi = True
            except ZlibError:
                self._eapi = False
            return self._eapi

    def eapi_login(self, origin, email, password, timeout=None):
        payload = self._eapi_json(
            "POST", origin, "/eapi/user/login",
            data={"email": email, "password": password}, timeout=timeout)
        user = payload.get("user") or {}
        user_id = _text(user.get("id"))
        key = _text(user.get("remix_userkey") or user.get("remix_user_key"))
        if not user_id or not key:
            raise ZlibError("accounts.login_failed", "EAPI login returned no credentials")
        return {
            "remix_userid": user_id,
            "remix_userkey": key,
            "cookies": {"remix_userid": user_id, "remix_userkey": key},
        }

    def eapi_profile(self, origin, account, timeout=None):
        payload = self._eapi_json("GET", origin, "/eapi/user/profile",
                                  account=account, timeout=timeout)
        user = payload.get("user") or {}
        today = user.get("downloads_today")
        if today is None:
            today = user.get("dailyDownloadsCount")
        limit = user.get("downloads_limit")
        if limit is None:
            limit = user.get("dailyDownloadLimit")
        today_value = _int_or_none(today)
        limit_value = _int_or_none(limit)
        remaining = None
        if today_value is not None and limit_value is not None:
            remaining = max(0, limit_value - today_value)
        return {
            "quota_today": today_value,
            "quota_limit": limit_value,
            "remaining": remaining,
            "email": _text(user.get("email")),
            "premium": bool(user.get("isPremium")),
            "user_id": _text(user.get("id")),
            "has_quota": limit_value is not None,
        }

    def eapi_search(self, origin, query, page, limit, filters=None, timeout=None):
        filters = filters or {}
        data = {
            "message": query,
            "page": str(max(1, int(page or 1))),
            "limit": str(max(1, min(int(limit or 20), 50))),
        }
        if filters.get("exact"):
            data["e"] = "1"
        if filters.get("year_from"):
            data["yearFrom"] = str(filters["year_from"])
        if filters.get("year_to"):
            data["yearTo"] = str(filters["year_to"])
        if filters.get("order"):
            data["order"] = str(filters["order"])
        for language in filters.get("languages") or []:
            if language:
                data.setdefault("languages[]", []).append(str(language).lower())
        for extension in filters.get("extensions") or []:
            if extension:
                data.setdefault("extensions[]", []).append(str(extension).lower())
        payload = self._eapi_json("POST", origin, "/eapi/book/search",
                                  data=data, timeout=timeout)
        books = []
        for raw in payload.get("books") or []:
            if not isinstance(raw, dict):
                continue
            books.append(_normalize_eapi_book(raw, origin))
        pagination = payload.get("pagination") or {}
        return {
            "books": books,
            "page": _int_or_none(pagination.get("current")) or int(page or 1),
            "total_pages": _int_or_none(pagination.get("total_pages")) or 0,
            "total_items": _int_or_none(pagination.get("total_items")) or 0,
            "source": "eapi",
            "origin": origin,
        }

    # ---------------------------------------------------------------- HTML

    def html_search(self, origin, query, page, timeout=None):
        params = {"page": str(max(1, int(page or 1)))}
        url = origin + "/s/" + quote(query)
        response = self._request_challenge_aware("GET", url, timeout=timeout)
        if response.status_code >= 400:
            raise ZlibError("search.http", "HTTP %d" % response.status_code)
        books = _parse_bookcards(response.text, origin)
        if not books:
            raise ZlibError("search.parse", "no z-bookcard found")
        return {
            "books": books,
            "page": int(page or 1),
            "total_pages": _html_total_pages(response.text, page),
            "total_items": 0,
            "source": "html",
            "origin": origin,
        }

    def html_login(self, origin, email, password, timeout=None):
        data = {
            "email": email,
            "password": password,
            "site_mode": "books",
            "action": "login",
            "redirectUrl": "/",
        }
        response = self._request_challenge_aware(
            "POST", origin + "/login", data=data, timeout=timeout)
        text = (response.text or "").lower()
        if "incorrect email or password" in text or "validation-error" in text:
            raise ZlibError("accounts.login_failed", "Incorrect email or password")
        cookies = {}
        host = urlsplit(origin).hostname or ""
        for cookie in self.doorway.session.cookies:
            if cookie.name in ("c_token", "c_time"):
                continue
            domain = (cookie.domain or "").lstrip(".").lower()
            if domain and host != domain and not host.endswith("." + domain):
                continue
            if cookie.value:
                cookies[cookie.name] = cookie.value
        if not cookies:
            raise ZlibError("accounts.login_failed", "no session cookie after HTML login")
        return {"cookies": cookies, "remix_userid": cookies.get("remix_userid", ""),
                "remix_userkey": cookies.get("remix_userkey", ""),
                "bsrv": cookies.get("bsrv", "")}

    def _html_download_path(self, origin, book, timeout=None):
        path = _text(book.get("download"))
        if path:
            return path
        href = _text(book.get("href"))
        if not href:
            raise ZlibError("download.no_link", "book has no href")
        response = self._request_challenge_aware(
            "GET", urljoin(origin + "/", href.lstrip("/")), timeout=timeout)
        if response.status_code >= 400:
            raise ZlibError("download.http", "HTTP %d" % response.status_code)
        soup = BeautifulSoup(response.text, "html.parser")
        for selector in ("a.addDownloadedBook", "a.dlButton"):
            link = soup.select_one(selector)
            if link is not None and link.get("href"):
                return link.get("href")
        match = re.search(r'href=["\'](/dl/[^"\']+)["\']', response.text)
        if match:
            return match.group(1)
        raise ZlibError("download.no_link", "no /dl/ link on book page")

    # ---------------------------------------------------------------- 下载

    def prepare_download(self, origin, book, account=None, timeout=None):
        """返回 {"url": ..., "response": optional_response}；URL 只在服务端。"""
        if account and book.get("id") and book.get("hash"):
            try:
                payload = self._eapi_json(
                    "GET", origin, "/eapi/book/%s/%s/file" % (book["id"], book["hash"]),
                    account=account, timeout=timeout)
                file_info = payload.get("file") or {}
                link = (file_info.get("downloadLink") or payload.get("downloadLink")
                        or payload.get("url") or payload.get("link"))
                if link:
                    target = _absolutize(origin, _text(link))
                    _validate_url(target)
                    return {"url": target}
            except ZlibError as err:
                if err.code in ("accounts.session_expired", "download.limit"):
                    raise
                # EAPI 文件端点不可用：继续走 HTML /dl/ 兜底
                logging.info("[zlib_client] EAPI file unavailable: %s", err.code)
        path = self._html_download_path(origin, book, timeout=timeout)
        return self._prepare_html_download(origin, path, account=account, timeout=timeout)

    def _prepare_html_download(self, origin, path, account=None, timeout=None):
        url = urljoin(origin + "/", path.lstrip("/"))
        _validate_url(url)
        if urlsplit(url).netloc.lower() != urlsplit(origin).netloc.lower():
            raise ZlibError("download.no_link", "download path escaped origin")
        cookies = self._account_cookies(account) if account else None
        self._request_lock.acquire()
        try:
            return self._prepare_html_download_locked(
                url, cookies, timeout, guest=(account is None))
        finally:
            self._request_lock.release()

    def _prepare_html_download_locked(self, url, cookies, timeout, guest=False):
        for _attempt in range(4):
            try:
                response = self.doorway.session.get(
                    url, allow_redirects=False, stream=True,
                    cookies=cookies, timeout=timeout or self.timeout)
            except ZlibError:
                raise
            except requests.RequestException as err:
                zerr = _find_zlib_error(err)
                if zerr is not None:
                    raise zerr
                raise ZlibError("download.http", str(err))
            if response.status_code in (403, 503) and _is_challenge(response):
                _solve_challenge(self.doorway.session, response)
                response.close()
                continue
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise ZlibError("download.http", "redirect without Location")
                target = urljoin(url, location)
                _validate_url(target)
                return {"url": target}
            if response.status_code >= 400:
                error = _download_http_error(response, guest=guest)
                response.close()
                raise error
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/html" in content_type:
                if _is_challenge(response):
                    _solve_challenge(self.doorway.session, response)
                    response.close()
                    continue
                error = _download_http_error(
                    response, default_code="download.no_link", guest=guest)
                response.close()
                raise error
            return {"url": response.url, "response": response}
        raise ZlibError("challenge.unsupported", "challenge did not pass")

    def stream_download(self, target, dest_path, *, max_bytes,
                        cancel_event=None, progress_cb=None, timeout=None):
        response = target.get("response") if isinstance(target, dict) else None
        url = target.get("url") if isinstance(target, dict) else target
        session = None
        if response is None:
            _validate_url(url)
            session = self._new_download_session()
            try:
                response = session.get(
                    url, stream=True, timeout=timeout or self.timeout)
            except requests.RequestException as err:
                session.close()
                zerr = _find_zlib_error(err)
                if zerr is not None:
                    raise zerr
                raise ZlibError("download.http", str(err))
            except Exception:
                session.close()
                raise
        try:
            if response.status_code >= 400:
                raise ZlibError("download.http", "HTTP %d" % response.status_code)
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/html" in content_type:
                raise ZlibError("download.html", "server returned HTML")
            length = response.headers.get("Content-Length") or ""
            total = int(length) if length.isdigit() else None
            if total is not None and total > max_bytes:
                raise ZlibError("download.too_large", "%d > %d" % (total, max_bytes))
            written = 0
            with open(dest_path, "wb") as handle:
                for chunk in response.iter_content(_CHUNK_SIZE):
                    if cancel_event is not None and cancel_event.is_set():
                        raise ZlibError("cancelled", "已取消")
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise ZlibError("download.too_large", "> %d" % max_bytes)
                    handle.write(chunk)
                    if progress_cb is not None:
                        progress_cb(written, total)
            return written
        finally:
            response.close()
            if session is not None:
                session.close()

    def health(self):
        started = time.time()
        origin = self.origin(force=True)
        eapi = self.probe_eapi(origin, force=True)
        return {
            "entry": self.doorway.entry_url,
            "final_origin": origin,
            "eapi": bool(eapi),
            "dns_mode": self.doorway.resolver.mode,
            "hops": list(self.doorway.hops),
            "latency": round(time.time() - started, 2),
        }


def _eapi_error(message):
    text = _text(message)
    low = text.lower()
    if "daily limit" in low or "download limit" in low or "limit reached" in low:
        return ZlibError("download.limit", text)
    if "please login" in low or "not authorized" in low or "invalid credentials" in low:
        return ZlibError("accounts.session_expired", text)
    if "authorization failed" in low:
        return ZlibError("accounts.login_failed", text)
    if "incorrect email or password" in low:
        return ZlibError("accounts.login_failed", text)
    return ZlibError("eapi.rejected", text)


def _download_http_error(response, default_code="download.http", guest=False):
    text = (response.text or "")[:20000].lower()
    if "ip address has already reached the download limit" in text:
        return ZlibError("download.limit", "guest IP limit reached")
    if "daily limit" in text or "download limit" in text or "download-limits-error" in text:
        return ZlibError("download.limit", "download limit reached")
    if "please login" in text or "log in" in text:
        # 游客被要求登录通常是 IP 限额的另一种页面；账号模式才是会话失效。
        if guest:
            return ZlibError("download.limit", "guest login required (IP limit)")
        return ZlibError("accounts.session_expired", "login required")
    return ZlibError(default_code, "HTTP %d" % response.status_code)


def _absolutize(origin, value):
    value = _text(value)
    if not value:
        return ""
    return urljoin(origin + "/", value.lstrip("/"))


def _normalize_eapi_book(raw, origin):
    cover = _text(raw.get("cover"))
    href = _text(raw.get("href"))
    download = _text(raw.get("dl") or raw.get("download"))
    return {
        "id": _text(raw.get("id")),
        "hash": _text(raw.get("hash")),
        "md5": _text(raw.get("md5")),
        "sha256": _text(raw.get("sha256")),
        "title": _text(raw.get("title")),
        "author": _text(raw.get("author")),
        "year": _text(raw.get("year")),
        "publisher": _text(raw.get("publisher")),
        "language": _text(raw.get("language")),
        "extension": _text(raw.get("extension")).lower(),
        "filesize": _text(raw.get("filesize")),
        "size": _text(raw.get("filesizeString")),
        "cover": cover,
        "href": href,
        "download": download,
        "origin": origin,
    }


def _parse_bookcards(html, origin):
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.find_all("z-bookcard")
    books = []
    for card in cards:
        def attr(name):
            value = card.get(name)
            return _text(value).strip()

        title_node = card.find(attrs={"slot": "title"})
        author_node = card.find(attrs={"slot": "author"})
        title = title_node.get_text(" ", strip=True) if title_node else ""
        author = author_node.get_text(" ", strip=True) if author_node else ""
        href = attr("href")
        if not href:
            continue
        books.append({
            "id": attr("id"),
            "hash": "",
            "md5": "",
            "sha256": "",
            "title": title,
            "author": author,
            "year": attr("year"),
            "publisher": attr("publisher"),
            "language": attr("language"),
            "extension": attr("extension").lower(),
            "filesize": "",
            "size": attr("filesize"),
            "cover": "",
            "href": href,
            "download": attr("download"),
            "origin": origin,
        })
    return books


def _html_total_pages(html, page):
    pages = [int(value) for value in re.findall(r"[?&]page=(\d+)", html or "")]
    if not pages:
        return int(page or 1)
    return max(max(pages), int(page or 1))


def sanitize_filename(name, fallback):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")[:120].strip()
    return cleaned or fallback
