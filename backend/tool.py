# -*- coding: utf-8 -*-
"""Z-Library 搜书入库：MyBooks 外部工具（Toolbox 工具包）后端。

入口固定为工具设置的 entry_url（默认 https://zlib.ch/）；最终域由 DoorwaySession
跟随跳转决定，不写死在代码里。搜索/下载优先 EAPI，失败回退 HTML 页面接口。
账号只保存会话 cookie/key，不保存邮箱密码。

路由挂载在 /api/toolbox/tool/zlibrary（由 manifest.json 的 api_routes 决定）。
"""
import functools
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid

import tornado.escape
import tornado.ioloop

from webserver.handlers.base import BaseHandler, is_admin, js
from webserver.i18n import _
from webserver.services import AsyncService
from webserver.toolbox.base_tool import BaseTool

from . import accounts as pool
from . import zlib_client
from .zlib_client import ZlibError

_WORK_TTL = 24 * 3600
_SEARCH_TTL = 2 * 3600
_MAX_SEARCH_CACHE = 600
_MAX_ACCOUNTS = 20
_MAX_CONCURRENT = 4
_JOB_KEEP = 10 * 60
_MAX_JOB_RECORDS = 40
_ENTRY_DEFAULT = zlib_client.DEFAULT_ENTRY
_CONFIG_KEYS = ("entry_url", "dns_mode", "timeout", "max_mb", "tag",
                "guest_enabled", "rotation", "search_limit")
_DNS_MODES = ("auto", "system", "doh")
_DEFAULT_CONFIG = {
    "entry_url": _ENTRY_DEFAULT,
    "dns_mode": "auto",
    "timeout": 30,
    "max_mb": 200,
    "tag": "",
    "guest_enabled": True,
    "rotation": "least_used",
    "search_limit": 20,
}
_LANGUAGE_OPTIONS = [
    ("zh", "Chinese"), ("zh-Hant", "Traditional Chinese"), ("en", "English"),
    ("ja", "Japanese"), ("ko", "Korean"), ("fr", "French"), ("de", "German"),
    ("es", "Spanish"), ("it", "Italian"), ("pt", "Portuguese"), ("ru", "Russian"),
    ("ar", "Arabic"), ("nl", "Dutch"), ("pl", "Polish"), ("tr", "Turkish"),
    ("vi", "Vietnamese"), ("id", "Indonesian"), ("hi", "Hindi"),
]
_EXTENSION_OPTIONS = ["epub", "pdf", "mobi", "azw3", "fb2", "djvu", "txt", "cbz", "cbr"]
_ORDER_OPTIONS = [
    ("", "Most relevant"), ("newest", "Newest"), ("oldest", "Oldest"),
    ("largest", "Largest"), ("smallest", "Smallest"),
]
# 下载期间会变化的账号字段；持久化时只按 id 合并这些字段，不能整份覆盖号池，
# 否则会丢掉下载过程中新增/修改/删除的账号。
_ACCOUNT_RUNTIME_FIELDS = (
    "email", "status", "quota_today", "quota_limit", "quota_updated_at",
    "last_used", "cooldown_until", "last_error",
)


def _result_row(name, status, reason="", detail="", book_id=None):
    return {"name": name, "status": status, "reason": reason,
            "detail": detail, "book_id": book_id}


def _filters_meta():
    return {
        "languages": [{"code": code, "label": label} for code, label in _LANGUAGE_OPTIONS],
        "extensions": list(_EXTENSION_OPTIONS),
        "orders": [{"value": value, "label": label} for value, label in _ORDER_OPTIONS],
    }


class ZlibraryTool(BaseTool):
    """从 zlib.ch 入口搜索并下载入库。"""

    service_item_name = "Z-Library 下载入库"

    _jobs = {}
    _jobs_lock = threading.RLock()
    _cancel_events = {}
    _account_lock = threading.RLock()
    _import_lock = threading.Lock()
    _client = None
    _client_key = None
    _client_lock = threading.Lock()
    _search_cache = {}
    _search_lock = threading.Lock()

    @staticmethod
    def info():
        return {
            "tool_id": "zlibrary",
            "name": "Z-Library 入库",
            "description": "从 zlib.ch 入口搜索 Z-Library 并下载入库，支持号池轮换（自用）",
            "revision": "0.1.2",
            "author": "黏菌",
            "publish_date": "2026-11-08",
            "repo_url": "https://github.com/shiningsprk-arch/tool_zlibrary",
        }

    # ------------------------------------------------------------- 设置

    def load_config(self):
        stored = self.api.storage.get_config() or {}
        config = dict(_DEFAULT_CONFIG)
        for key in _CONFIG_KEYS:
            if key in stored:
                config[key] = stored[key]
        config["entry_url"] = _normalize_entry(config.get("entry_url"))
        config["dns_mode"] = (config.get("dns_mode")
                              if config.get("dns_mode") in _DNS_MODES else "auto")
        config["timeout"] = _bounded_int(config.get("timeout"), 5, 120, 30)
        config["max_mb"] = _bounded_int(config.get("max_mb"), 1, 2048, 200)
        config["search_limit"] = _bounded_int(config.get("search_limit"), 5, 50, 20)
        config["tag"] = str(config.get("tag") or "").strip()[:60]
        config["guest_enabled"] = bool(config.get("guest_enabled", True))
        config["rotation"] = (config.get("rotation")
                              if config.get("rotation") in ("least_used", "round_robin")
                              else "least_used")
        raw_accounts = stored.get("accounts") or []
        config["accounts"] = [pool.normalize_account(item)
                              for item in raw_accounts if isinstance(item, dict)][:_MAX_ACCOUNTS]
        return config

    def public_config(self, config=None):
        config = config or self.load_config()
        data = dict((key, config[key]) for key in _CONFIG_KEYS)
        data["filters"] = _filters_meta()
        data["accounts"] = [pool.public_account(item) for item in config["accounts"]]
        return data

    def save_config(self, patch):
        with self._account_lock:
            return self._save_config(patch)

    def _save_config(self, patch):
        config = self.load_config()
        for key in _CONFIG_KEYS:
            if key not in patch:
                continue
            value = patch[key]
            if key == "entry_url":
                config[key] = _normalize_entry(value)
            elif key == "dns_mode":
                config[key] = value if value in _DNS_MODES else "auto"
            elif key in ("timeout", "max_mb", "search_limit"):
                limits = {"timeout": (5, 120, 30), "max_mb": (1, 2048, 200),
                          "search_limit": (5, 50, 20)}[key]
                config[key] = _bounded_int(value, *limits)
            elif key == "tag":
                config[key] = str(value or "").strip()[:60]
            elif key == "guest_enabled":
                config[key] = bool(value)
            elif key == "rotation":
                config[key] = value if value in ("least_used", "round_robin") else "least_used"
        self._persist(config)
        self.reset_client()
        return self.public_config(config)

    def save_accounts(self, account_list):
        with self._account_lock:
            return self._save_accounts(account_list)

    def _save_accounts(self, account_list):
        config = self.load_config()
        config["accounts"] = [pool.normalize_account(item)
                              for item in account_list if isinstance(item, dict)][:_MAX_ACCOUNTS]
        self._persist(config)
        return config

    def _persist(self, config):
        stored = dict(config)
        stored.pop("filters", None)
        stored["accounts"] = [pool.normalize_account(item) for item in config["accounts"]]
        self.api.storage.set_config(stored)

    def _persist_account_state(self, account):
        """只把运行期字段合并回号池，避免下载任务的旧快照覆盖新账号。"""
        with self._account_lock:
            config = self.load_config()
            fresh = pool.find_account(config["accounts"], account.get("id"))
            if not fresh:
                return
            for key in _ACCOUNT_RUNTIME_FIELDS:
                if key == "email" and not account.get(key):
                    continue
                if key in account:
                    fresh[key] = account[key]
            self._persist(config)

    # ------------------------------------------------------------- 客户端

    @classmethod
    def reset_client(cls):
        with cls._client_lock:
            cls._client = None
            cls._client_key = None

    def get_client(self, config=None):
        config = config or self.load_config()
        key = (config["entry_url"], config["dns_mode"], config["timeout"])
        with self._client_lock:
            if self._client is None or self._client_key != key:
                type(self)._client = zlib_client.ZlibClient(
                    config["entry_url"], config["dns_mode"], config["timeout"])
                type(self)._client_key = key
            return self._client

    def health_sync(self):
        return self.get_client().health()

    # ------------------------------------------------------------- 搜索缓存

    def _cache_books(self, books):
        now = time.time()
        tokens = []
        with self._search_lock:
            expired = [token for token, item in self._search_cache.items()
                       if item[1] <= now]
            for token in expired:
                self._search_cache.pop(token, None)
            if len(self._search_cache) > _MAX_SEARCH_CACHE:
                oldest = sorted(self._search_cache.items(), key=lambda pair: pair[1][1])
                for token, _item in oldest[:len(self._search_cache) - _MAX_SEARCH_CACHE]:
                    self._search_cache.pop(token, None)
            for book in books:
                token = uuid.uuid4().hex[:16]
                self._search_cache[token] = (dict(book), now + _SEARCH_TTL)
                public = {
                    "book_token": token,
                    "title": book.get("title") or "",
                    "author": book.get("author") or "",
                    "year": book.get("year") or "",
                    "publisher": book.get("publisher") or "",
                    "language": book.get("language") or "",
                    "extension": (book.get("extension") or "").lower(),
                    "size": book.get("size") or "",
                    "cover": book.get("cover") or "",
                }
                tokens.append(public)
        return tokens

    def load_book(self, token):
        if not re.fullmatch(r"[a-f0-9]{16}", token or ""):
            raise ZlibError("params.invalid", "bad book token")
        with self._search_lock:
            item = self._search_cache.get(token)
            if not item:
                raise ZlibError("book.expired", "search result expired")
            book, expires = item
            if expires <= time.time():
                self._search_cache.pop(token, None)
                raise ZlibError("book.expired", "search result expired")
            return dict(book)

    @AsyncService.register_function
    def search_sync(self, q, page, filters, timeout):
        config = self.load_config()
        client = self.get_client(config)
        origin = client.origin()
        filters = filters or {}
        try:
            if client.probe_eapi(origin):
                result = client.eapi_search(
                    origin, q, page, config["search_limit"], filters,
                    timeout=min(config["timeout"], 60))
            else:
                result = client.html_search(origin, q, page, timeout=min(config["timeout"], 60))
        except ZlibError as err:
            if err.code in ("eapi.http", "eapi.rejected") and client.probe_eapi(origin):
                with client._lock:
                    client._eapi = False
                result = client.html_search(origin, q, page, timeout=min(config["timeout"], 60))
            else:
                raise
        books = self._cache_books(result.get("books") or [])
        return {
            "books": books,
            "page": result.get("page") or page,
            "total_pages": result.get("total_pages") or 0,
            "total_items": result.get("total_items") or 0,
            "source": result.get("source") or "",
            "origin": result.get("origin") or origin,
        }

    # ------------------------------------------------------------- 工作目录

    def new_work_dir(self, ttl):
        token = uuid.uuid4().hex[:16]
        self._gc_old_work_dirs(ttl)
        return token, self.api.storage.get_work_dir(token)

    def _gc_old_work_dirs(self, ttl):
        try:
            root = self.api.storage.get_work_dir()
            if not os.path.isdir(root):
                return
            now = time.time()
            for name in os.listdir(root):
                path = os.path.join(root, name)
                if not os.path.isdir(path):
                    continue
                try:
                    if now - os.path.getmtime(path) > ttl:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    continue
        except OSError as err:
            logging.warning("[ZlibraryTool] GC failed: %s", err)

    # ------------------------------------------------------------- 任务状态

    @staticmethod
    def _is_active(job):
        return bool(job) and job.get("status") in ("starting", "running")

    @classmethod
    def _gc_jobs_locked(cls):
        now = time.time()
        expired = [job_id for job_id, job in cls._jobs.items()
                   if not cls._is_active(job)
                   and now - job.get("updated_at", 0) > _JOB_KEEP]
        for job_id in expired:
            cls._jobs.pop(job_id, None)
            cls._cancel_events.pop(job_id, None)
        if len(cls._jobs) <= _MAX_JOB_RECORDS:
            return
        ordered = sorted(cls._jobs.items(),
                         key=lambda pair: pair[1].get("created_at", 0))
        for job_id, job in ordered:
            if len(cls._jobs) <= _MAX_JOB_RECORDS:
                break
            if not cls._is_active(job):
                cls._jobs.pop(job_id, None)
                cls._cancel_events.pop(job_id, None)

    @classmethod
    def is_running(cls):
        with cls._jobs_lock:
            return any(cls._is_active(job) for job in cls._jobs.values())

    @classmethod
    def active_count(cls):
        with cls._jobs_lock:
            return len([job for job in cls._jobs.values() if cls._is_active(job)])

    @classmethod
    def snapshot(cls, job_id=None):
        with cls._jobs_lock:
            if job_id:
                job = cls._jobs.get(job_id)
                return dict(job) if job else {}
            jobs = sorted(cls._jobs.values(),
                          key=lambda job: job.get("created_at", 0))
            if not jobs:
                return {}
            snap = dict(jobs[-1])
            snap["results"] = list(snap.get("results") or [])
            return snap

    @classmethod
    def snapshot_jobs(cls):
        with cls._jobs_lock:
            cls._gc_jobs_locked()
            jobs = sorted(cls._jobs.values(),
                          key=lambda job: job.get("created_at", 0))
            return [dict(job) for job in jobs]

    @classmethod
    def _update(cls, job_id, **fields):
        with cls._jobs_lock:
            job = cls._jobs.get(job_id)
            if not job:
                return
            job.update(fields)
            job["updated_at"] = time.time()

    @classmethod
    def _begin(cls, job_id, token, title):
        with cls._jobs_lock:
            cls._gc_jobs_locked()
            active = [job for job in cls._jobs.values() if cls._is_active(job)]
            if len(active) >= _MAX_CONCURRENT:
                return False
            now = time.time()
            cls._jobs[job_id] = {
                "job_id": job_id, "book_token": token, "task_id": None,
                "status": "starting", "progress": 0, "stage": "prepare",
                "current": title or "", "error": "", "detail": "",
                "downloaded": 0, "total": None, "book_id": None,
                "results": [], "account": "",
                "created_at": now, "updated_at": now,
            }
            cls._cancel_events[job_id] = threading.Event()
            return True

    @classmethod
    def _discard(cls, job_id):
        with cls._jobs_lock:
            cls._jobs.pop(job_id, None)
            cls._cancel_events.pop(job_id, None)

    @classmethod
    def _cancel_event(cls, job_id):
        with cls._jobs_lock:
            return cls._cancel_events.get(job_id)

    # ------------------------------------------------------------- 账号操作

    def _origin(self, client):
        return client.origin()

    def _profile_account(self, client, origin, account, config):
        try:
            profile = client.eapi_profile(origin, account,
                                          timeout=min(config["timeout"], 45))
        except ZlibError as err:
            if err.code in ("accounts.session_expired", "accounts.login_failed"):
                pool.mark_error(account, err.code, err.message)
                return False
            if err.code in ("eapi.http", "eapi.rejected"):
                return True
            raise
        pool.apply_profile(account, profile)
        if profile.get("email") and not account.get("email"):
            account["email"] = profile["email"]
        return True

    def account_action(self, body):
        with self._account_lock:
            return self._account_action(body)

    def _account_action(self, body):
        action = str(body.get("action") or "").strip()
        config = self.load_config()
        client = self.get_client(config)
        origin = self._origin(client)
        account_list = config["accounts"]
        now = time.time()

        if action == "add":
            mode = str(body.get("mode") or "cookie").strip()
            label = str(body.get("label") or "").strip()
            email = str(body.get("email") or "").strip()
            password = str(body.get("password") or "")
            if mode == "password":
                if not email or not password:
                    raise ZlibError("params.missing", "email/password required")
                login = None
                try:
                    login = client.eapi_login(origin, email, password,
                                              timeout=min(config["timeout"], 45))
                except ZlibError as err:
                    if err.code not in ("accounts.login_failed", "eapi.http", "eapi.rejected"):
                        raise
                if login is None:
                    login = client.html_login(origin, email, password,
                                              timeout=min(config["timeout"], 45))
                account = pool.new_account(label, email=email)
                pool.merge_login(account, login)
            else:
                cookies = body.get("cookies") if isinstance(body.get("cookies"), dict) else {}
                user_id = str(body.get("remix_userid") or "").strip()
                user_key = str(body.get("remix_userkey") or "").strip()
                bsrv = str(body.get("bsrv") or "").strip()
                if not (user_id or user_key or cookies):
                    raise ZlibError("params.missing", "cookie/key required")
                account = pool.new_account(
                    label, cookies=cookies, remix_userid=user_id,
                    remix_userkey=user_key, bsrv=bsrv, email=email)
                if user_id:
                    account["cookies"]["remix_userid"] = user_id
                if user_key:
                    account["cookies"]["remix_userkey"] = user_key
                if bsrv:
                    account["cookies"]["bsrv"] = bsrv
            pool.apply_profile(account, {})
            ok = self._profile_account(client, origin, account, config)
            if not ok and account.get("status") == pool.STATUS_INVALID:
                raise ZlibError("accounts.invalid", "会话无效，请重新登录或导入 cookie")
            account["quota_updated_at"] = now
            account_list.append(account)
            self.save_accounts(account_list)
            return {"account": pool.public_account(account), "accounts": [
                pool.public_account(item) for item in account_list]}

        if action == "update":
            account = pool.find_account(account_list, str(body.get("id") or ""))
            if not account:
                raise ZlibError("params.invalid", "account not found")
            if "label" in body:
                account["label"] = str(body.get("label") or "").strip()[:60]
            if "enabled" in body:
                account["enabled"] = bool(body.get("enabled"))
            if "remix_userid" in body or "remix_userkey" in body or "bsrv" in body:
                for key in ("remix_userid", "remix_userkey", "bsrv"):
                    if key in body:
                        account[key] = str(body.get(key) or "").strip()
                        if account[key]:
                            account.setdefault("cookies", {})[key] = account[key]
                account["status"] = pool.STATUS_UNKNOWN
            self.save_accounts(account_list)
            return {"account": pool.public_account(account), "accounts": [
                pool.public_account(item) for item in account_list]}

        if action == "delete":
            account = pool.find_account(account_list, str(body.get("id") or ""))
            if not account:
                raise ZlibError("params.invalid", "account not found")
            account_list = [item for item in account_list if item["id"] != account["id"]]
            self.save_accounts(account_list)
            return {"accounts": [pool.public_account(item) for item in account_list]}

        if action == "test":
            account = pool.find_account(account_list, str(body.get("id") or ""))
            if not account:
                raise ZlibError("params.invalid", "account not found")
            self._profile_account(client, origin, account, config)
            self.save_accounts(account_list)
            return {"account": pool.public_account(account), "accounts": [
                pool.public_account(item) for item in account_list]}

        raise ZlibError("params.invalid", "unknown account action")

    # ------------------------------------------------------------- 下载任务

    def _pick_download(self, client, origin, book, config, account_id=None):
        # 账号额度/会话是按账号共享的资源，并发任务必须串行挑账号；
        # 真正的文件流在锁外并行。
        with self._account_lock:
            return self._pick_download_locked(
                client, origin, book, config, account_id=account_id)

    def _pick_download_locked(self, client, origin, book, config, account_id=None):
        # 必须用锁内重新读取的号池，调用方在下载开始时拿到的 config 可能已经过期。
        account_list = self.load_config()["accounts"]
        chosen = []
        if account_id:
            account = pool.find_account(account_list, account_id)
            if not account:
                raise ZlibError("accounts.invalid", "account not found")
            chosen = [account]
        else:
            remaining = list(account_list)
            while True:
                account = pool.select_account(remaining)
                if account is None:
                    break
                chosen.append(account)
                remaining = [item for item in remaining if item["id"] != account["id"]]
        last_error = None
        for account in chosen:
            if not account.get("enabled"):
                continue
            try:
                # 先占用 last_used，让并发的下一个任务挑到别的号；否则所有任务
                # 都会在下载完成前选中同一个“最久未用”账号。
                account["last_used"] = time.time()
                self._persist_account_state(account)
                if not self._profile_account(client, origin, account, config):
                    self._persist_account_state(account)
                    continue
                self._persist_account_state(account)
                target = client.prepare_download(
                    origin, book, account=account,
                    timeout=min(config["timeout"], 60))
                return target, account
            except ZlibError as err:
                if err.code in ("accounts.session_expired", "accounts.login_failed",
                                "download.limit"):
                    pool.mark_error(account, err.code, err.message)
                    self._persist_account_state(account)
                    last_error = err
                    continue
                raise
        if config.get("guest_enabled"):
            target = client.prepare_download(
                origin, book, account=None, timeout=min(config["timeout"], 60))
            return target, None
        if last_error is not None:
            raise last_error
        raise ZlibError("accounts.exhausted", "no usable account and guest mode is off")

    @AsyncService.register_service
    def run_download(self, user_id, work_dir, book_token, job_id, account_id=None):
        """服务队列只负责派发；每个任务一条线程，避免单队列把下载串行化。"""
        args = (user_id, work_dir, book_token, job_id, account_id)
        try:
            threaded = bool(AsyncService().async_mode())
        except Exception:
            threaded = False
        if not threaded:
            self._download_job(*args)
            return
        thread = threading.Thread(
            target=self._job_thread, args=args, daemon=True,
            name="zlibrary-download-%s" % job_id)
        try:
            thread.start()
        except Exception as err:
            logging.exception("[ZlibraryTool] failed to start download thread")
            shutil.rmtree(work_dir, ignore_errors=True)
            self._fail(job_id, None, user_id, "task.failed", str(err))

    def _job_thread(self, user_id, work_dir, book_token, job_id, account_id=None):
        scoped = getattr(self, "scoped_session", None)
        try:
            self._download_job(user_id, work_dir, book_token, job_id, account_id)
        except BaseException as err:
            logging.exception("[ZlibraryTool] download thread aborted")
            snap = self.snapshot(job_id)
            shutil.rmtree(work_dir, ignore_errors=True)
            self._fail(job_id, snap.get("task_id"), user_id, "task.failed", str(err))
        finally:
            remove = getattr(scoped, "remove", None)
            if callable(remove):
                try:
                    remove()
                except Exception:
                    pass

    def _report_task(self, task_id, percent, data=None):
        if task_id is None:
            return
        try:
            self.api.tasks.update_progress(task_id, percent,
                                           progress_data=data or {})
        except Exception as err:
            logging.warning("[ZlibraryTool] task progress update failed: %s", err)

    def _download_job(self, user_id, work_dir, book_token, job_id,
                      account_id=None):
        task_id = None
        cancel_event = self._cancel_event(job_id) or threading.Event()
        try:
            task_id = self.api.tasks.create_task({"stage": "prepare"})
            self._update(job_id, task_id=task_id, status="running",
                         stage="downloading", progress=3)
            self._report_task(task_id, 3, {"stage": "downloading"})
            config = self.load_config()
            client = self.get_client(config)
            book = self.load_book(book_token)
            origin = client.origin()
            title = (book.get("title") or "").strip()
            author = (book.get("author") or "").strip()
            ext = str(book.get("extension") or "epub").lower()
            if not re.fullmatch(r"[a-z0-9]{1,8}", ext):
                ext = "epub"
            target, used_account = self._pick_download(
                client, origin, book, config, account_id=account_id)
            filename = "%s.%s" % (
                zlib_client.sanitize_filename(title, book.get("id") or "book"), ext)
            dest = os.path.join(work_dir, filename)
            last_report = [0.0]

            def on_progress(written, total):
                percent = 5 + int(88 * written / total) if total else 30
                percent = min(percent, 93)
                self._update(job_id, progress=percent,
                             downloaded=written, total=total)
                now = time.time()
                if now - last_report[0] >= 0.5 or percent >= 93:
                    last_report[0] = now
                    self._report_task(task_id, percent, {
                        "stage": "downloading", "downloaded": written,
                        "total": total})

            self._update(job_id, stage="downloading", current=title,
                         account=(used_account or {}).get("label", "游客"))
            size = client.stream_download(
                target, dest, max_bytes=config["max_mb"] * (1024 ** 2),
                cancel_event=cancel_event, progress_cb=on_progress,
                timeout=config["timeout"])
            if cancel_event.is_set():
                self._finish_cancelled(job_id, task_id, user_id)
                return
            self._update(job_id, stage="importing", progress=96,
                         downloaded=size, total=size)
            self._report_task(task_id, 96, {"stage": "importing"})
            with self._import_lock:
                book_id = self.api.calibre.import_file(
                    user_id, dest, title, [author] if author else [],
                    delete_after_import=True)
                self._apply_tag(book_id, config.get("tag", ""))
            if used_account is not None:
                with self._account_lock:
                    pool.mark_success(used_account)
                    try:
                        self._profile_account(client, origin, used_account, config)
                    except Exception as err:
                        logging.warning(
                            "[ZlibraryTool] quota refresh failed: %s", err)
                    self._persist_account_state(used_account)
            self._report_task(task_id, 100,
                              {"stage": "done", "book_id": book_id})
            self.api.tasks.complete_task(task_id)
            self._update(job_id, status="completed", progress=100,
                         stage="done", book_id=book_id, current=title,
                         account=(used_account or {}).get("label", "游客"),
                         results=[_result_row(filename, "imported",
                                              book_id=book_id)])
        except ZlibError as err:
            if err.code == "cancelled":
                self._finish_cancelled(job_id, task_id, user_id)
                return
            self._fail(job_id, task_id, user_id, err.code, err.message)
        except Exception as err:
            logging.exception("[ZlibraryTool] task crashed")
            self._fail(job_id, task_id, user_id, "task.failed", str(err))
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _fail(self, job_id, task_id, user_id, code, detail):
        if task_id is not None:
            self._report_task(task_id, 100, {"stage": "failed"})
            self.api.tasks.complete_task(task_id, error_message=detail or code)
        self._update(job_id, status="failed", error=code, detail=detail,
                     stage="done", progress=100)
        self.add_msg(user_id, "danger", _("下载/入库失败：%s") % (detail or code))

    def _finish_cancelled(self, job_id, task_id, user_id):
        if task_id is not None:
            self._report_task(task_id, 100, {"stage": "cancelled"})
            self.api.tasks.complete_task(task_id, error_message=_("已取消"))
        self._update(job_id, status="cancelled", error="cancelled",
                     stage="done", progress=100)
        self.add_msg(user_id, "warning", _("下载已取消"))

    def _apply_tag(self, book_id, tag):
        if not tag:
            return
        try:
            mi = self.api.calibre.get_metadata(book_id)
            tags = list(getattr(mi, "tags", None) or [])
            if tag not in tags:
                mi.tags = tags + [tag]
                self.api.calibre.set_metadata(book_id, mi, force_changes=True)
        except Exception as err:
            logging.warning("[ZlibraryTool] apply tag failed: %s", err)


def _normalize_entry(value):
    value = str(value or _ENTRY_DEFAULT).strip() or _ENTRY_DEFAULT
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    if not value.endswith("/"):
        value += "/"
    return value


def _bounded_int(value, low, high, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


# --------------------------------------------------------------------- 路由

def _json_body(handler):
    try:
        data = tornado.escape.json_decode(handler.request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class ConfigHandler(BaseHandler):
    @js
    @is_admin
    def get(self):
        tool = ZlibraryTool()
        return {"err": "ok", "data": tool.public_config()}

    @js
    @is_admin
    def post(self):
        tool = ZlibraryTool()
        return {"err": "ok", "data": tool.save_config(_json_body(self))}


class HealthHandler(BaseHandler):
    @js
    @is_admin
    async def get(self):
        tool = ZlibraryTool()
        loop = tornado.ioloop.IOLoop.current()
        try:
            data = await loop.run_in_executor(None, tool.health_sync)
        except ZlibError as err:
            return {"err": err.code, "msg": err.message}
        return {"err": "ok", "data": data}


class AccountsHandler(BaseHandler):
    @js
    @is_admin
    def get(self):
        tool = ZlibraryTool()
        config = tool.load_config()
        return {"err": "ok", "data": {
            "accounts": [pool.public_account(item) for item in config["accounts"]]}}

    @js
    @is_admin
    async def post(self):
        body = _json_body(self)
        tool = ZlibraryTool()
        loop = tornado.ioloop.IOLoop.current()
        try:
            data = await loop.run_in_executor(
                None, functools.partial(tool.account_action, body))
        except ZlibError as err:
            return {"err": err.code, "msg": err.message}
        return {"err": "ok", "data": data}


class SearchHandler(BaseHandler):
    @js
    @is_admin
    async def post(self):
        body = _json_body(self)
        q = str(body.get("q") or "").strip()
        if not q:
            return {"err": "params.missing", "msg": _("请输入搜索词")}
        try:
            page = max(1, min(int(body.get("page") or 1), 100))
        except (TypeError, ValueError):
            page = 1
        filters = {}
        for key in ("languages", "extensions"):
            value = body.get(key)
            if isinstance(value, list):
                filters[key] = [str(item) for item in value if item]
        for key in ("year_from", "year_to"):
            value = body.get(key)
            if value not in (None, ""):
                try:
                    filters[key] = max(0, min(int(value), 2200))
                except (TypeError, ValueError):
                    pass
        if body.get("order"):
            filters["order"] = str(body["order"])
        if body.get("exact"):
            filters["exact"] = True
        tool = ZlibraryTool()
        config = tool.load_config()
        loop = tornado.ioloop.IOLoop.current()
        try:
            data = await loop.run_in_executor(
                None, functools.partial(tool.search_sync, q, page, filters,
                                        config["timeout"]))
        except ZlibError as err:
            return {"err": err.code, "msg": err.message}
        return {"err": "ok", "data": data}


class DownloadHandler(BaseHandler):
    @js
    @is_admin
    def post(self):
        body = _json_body(self)
        token = str(body.get("book_token") or "")
        account_id = str(body.get("account_id") or "").strip() or None
        tool = ZlibraryTool()
        try:
            book = tool.load_book(token)
        except ZlibError as err:
            return {"err": err.code, "msg": err.message}
        tool._gc_old_work_dirs(_WORK_TTL)
        job_id = uuid.uuid4().hex[:16]
        work_dir = tool.api.storage.get_work_dir(job_id)
        os.makedirs(work_dir, exist_ok=True)
        if not ZlibraryTool._begin(job_id, token, book.get("title") or ""):
            shutil.rmtree(work_dir, ignore_errors=True)
            return {"err": "task.limit",
                    "msg": _("同时下载任务已达上限（%d 个）") % _MAX_CONCURRENT}
        try:
            tool.run_download(self.current_user.id, work_dir, token, job_id,
                              account_id=account_id)
        except Exception:
            ZlibraryTool._discard(job_id)
            shutil.rmtree(work_dir, ignore_errors=True)
            raise
        return {"err": "ok", "data": {"job_id": job_id},
                "msg": _("下载任务已启动")}


class ProgressHandler(BaseHandler):
    @js
    @is_admin
    def get(self):
        jobs = ZlibraryTool.snapshot_jobs()
        latest = jobs[-1] if jobs else {}
        data = dict(latest)
        data["jobs"] = jobs
        data["active"] = len([job for job in jobs
                              if job.get("status") in ("starting", "running")])
        if not data["jobs"]:
            data = {"status": "idle", "jobs": [], "active": 0}
        return {"err": "ok", "msg": "", "data": data}


class CancelHandler(BaseHandler):
    @js
    @is_admin
    def post(self):
        body = _json_body(self)
        job_id = str(body.get("job_id") or "").strip()
        with ZlibraryTool._jobs_lock:
            active = {
                jid: event
                for jid, event in ZlibraryTool._cancel_events.items()
                if ZlibraryTool._is_active(ZlibraryTool._jobs.get(jid))
            }
            if job_id:
                event = active.get(job_id)
                if event is None:
                    return {"err": "task.not_running",
                            "msg": _("当前没有进行中的下载任务")}
                event.set()
                return {"err": "ok", "msg": _("已请求取消")}
            if not active:
                return {"err": "task.not_running",
                        "msg": _("当前没有进行中的下载任务")}
            for event in active.values():
                event.set()
            return {"err": "ok", "msg": _("已请求取消")}
