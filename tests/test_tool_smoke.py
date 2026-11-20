# -*- coding: utf-8 -*-
"""ZlibraryTool 冒烟：stub 宿主 + 假客户端，离线跑通设置、搜索缓存、号池、
下载→入库→补标签、取消与失败路径。"""
import base64
import importlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(ROOT, "backend")


def _install_stubs():
    webserver = types.ModuleType("webserver")
    handlers = types.ModuleType("webserver.handlers")
    handlers_base = types.ModuleType("webserver.handlers.base")

    class BaseHandler(object):
        pass

    handlers_base.BaseHandler = BaseHandler
    handlers_base.js = lambda fn: fn
    handlers_base.is_admin = lambda fn: fn
    i18n = types.ModuleType("webserver.i18n")
    i18n._ = lambda s: s
    services = types.ModuleType("webserver.services")

    class AsyncService(object):
        @staticmethod
        def register_service(fn):
            return fn

        @staticmethod
        def register_function(fn):
            return fn

    services.AsyncService = AsyncService
    background_service = types.ModuleType("webserver.services.background_service")

    class BackgroundTask(object):
        SERVICE_TYPE_OTHER = "other"

    class BackgroundService(object):
        def update_task(self, **kwargs):
            raise AssertionError("测试不应直接创建宿主后台任务")

    background_service.BackgroundTask = BackgroundTask
    background_service.BackgroundService = BackgroundService
    services.background_service = background_service
    toolbox = types.ModuleType("webserver.toolbox")
    base_tool = types.ModuleType("webserver.toolbox.base_tool")
    core_api = types.ModuleType("webserver.toolbox.core_api")

    class BaseTool(object):
        SUPPORTED_FORMATS = {"epub", "pdf", "azw3", "mobi", "txt"}
        TOOL_SERVICE_TYPE = None
        service_item_name = ""

        def __init__(self):
            self.api = None

        def add_msg(self, user_id, status, msg):
            pass

    class CoreAPI(object):
        pass

    base_tool.BaseTool = BaseTool
    core_api.CoreAPI = CoreAPI
    webserver.handlers = handlers
    handlers.base = handlers_base
    webserver.i18n = i18n
    webserver.services = services
    webserver.toolbox = toolbox
    toolbox.base_tool = base_tool
    toolbox.core_api = core_api
    for name, mod in (("webserver", webserver), ("webserver.handlers", handlers),
                      ("webserver.handlers.base", handlers_base),
                      ("webserver.i18n", i18n), ("webserver.services", services),
                      ("webserver.services.background_service", background_service),
                      ("webserver.toolbox", toolbox),
                      ("webserver.toolbox.base_tool", base_tool),
                      ("webserver.toolbox.core_api", core_api)):
        sys.modules[name] = mod


_install_stubs()
_PKG = types.ModuleType("zlibrary_backend_pkg")
_PKG.__path__ = [BACKEND_DIR]
sys.modules["zlibrary_backend_pkg"] = _PKG
tool_mod = importlib.import_module("zlibrary_backend_pkg.tool")
ZlibraryTool = tool_mod.ZlibraryTool
pool = tool_mod.pool
zlib_client = tool_mod.zlib_client


class FakeMI(object):
    def __init__(self, title="", authors=None, tags=None):
        self.title = title
        self.authors = list(authors or [])
        self.tags = list(tags or [])


class FakeCalibre(object):
    def __init__(self):
        self.imported = []
        self.meta = {}
        self.set_calls = []

    def import_file(self, user_id, file_path, title, authors, *, delete_after_import=True):
        with open(file_path, "rb") as f:
            self.imported.append((f.read(), title, list(authors)))
        self.meta[len(self.imported)] = FakeMI(authors=["Unknown"])
        if delete_after_import and os.path.exists(file_path):
            os.remove(file_path)
        return len(self.imported)

    def get_metadata(self, book_id, **kwargs):
        return self.meta[book_id]

    def set_metadata(self, book_id, mi, force_changes=True):
        self.set_calls.append(book_id)
        self.meta[book_id] = mi


class FakeAPI(object):
    def __init__(self, root, calibre):
        self._root = root
        self.calibre = calibre
        self._config = {}
        self.storage = self
        self.tasks = self
        self.messages = self
        self.completed = []
        self.msgs = []
        self.created = 0
        self.progress_updates = []

    def get_work_dir(self, unique_key=None):
        path = os.path.join(self._root, unique_key or "root")
        os.makedirs(path, exist_ok=True)
        return path

    def cleanup_work_dir(self, work_dir):
        pass

    def get_config(self):
        return json.loads(json.dumps(self._config))

    def set_config(self, data):
        self._config = json.loads(json.dumps(data))

    def create_task(self, progress_data=None):
        self.created += 1
        return self.created

    def update_progress(self, task_id, progress, progress_data=None):
        self.progress_updates.append((task_id, progress, progress_data))

    def complete_task(self, task_id, error_message=None):
        self.completed.append(error_message)

    def send_message(self, user_id, msg, status="info"):
        self.msgs.append((status, msg))


class FakeClient(object):
    def __init__(self):
        self.profile_calls = 0
        self.login_calls = 0
        self.prepared = []
        self.prepare_errors = []
        self.stream_error = None
        self.profile_error = None
        self.login_error = None
        self.cancel_on_stream = False
        self.written = b"PK\x03\x04fake-epub"
        self.barrier = None
        self.on_stream = None
        self.last_dest = None
        self.parallel = 0
        self.max_parallel = 0
        self.parallel_lock = threading.Lock()

    def origin(self):
        return "https://origin.example"

    def probe_eapi(self, origin, force=False):
        return True

    def eapi_search(self, origin, query, page, limit, filters=None, timeout=None):
        return {
            "books": [{
                "id": "123", "hash": "abcdef", "md5": "", "sha256": "",
                "title": "Test Book", "author": "Test Author",
                "year": "2020", "publisher": "P", "language": "English",
                "extension": "epub", "filesize": "100", "size": "100 KB",
                "cover": "", "href": "/book/123/x.html",
                "download": "/dl/token", "origin": origin,
            }],
            "page": page, "total_pages": 3, "total_items": 1,
            "source": "eapi", "origin": origin,
        }

    def eapi_profile(self, origin, account, timeout=None):
        self.profile_calls += 1
        if self.profile_error is not None:
            raise self.profile_error
        return {"quota_today": 0, "quota_limit": 10, "remaining": 10,
                "email": account.get("email", ""), "premium": False}

    def eapi_login(self, origin, email, password, timeout=None):
        self.login_calls += 1
        if self.login_error is not None:
            raise self.login_error
        if password != "secret-password":
            raise zlib_client.ZlibError("accounts.login_failed", "bad")
        self.profile_error = None
        return {"remix_userid": "1001", "remix_userkey": "key-abcdef",
                "cookies": {"remix_userid": "1001", "remix_userkey": "key-abcdef"}}

    def html_login(self, origin, email, password, timeout=None):
        raise zlib_client.ZlibError("accounts.login_failed", "bad")

    def prepare_download(self, origin, book, account=None, timeout=None):
        self.prepared.append((book.get("id"), (account or {}).get("id")))
        if self.prepare_errors:
            raise self.prepare_errors.pop(0)
        return {"url": "https://cdn.example/book.epub"}

    def stream_download(self, target, dest, *, max_bytes, cancel_event=None,
                        progress_cb=None, timeout=None):
        if self.stream_error is not None:
            raise self.stream_error
        if self.cancel_on_stream:
            cancel_event.set()
            return 0
        callback, self.on_stream = self.on_stream, None
        if callback is not None:
            callback()
        self.last_dest = dest
        with self.parallel_lock:
            self.parallel += 1
            self.max_parallel = max(self.max_parallel, self.parallel)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            with open(dest, "wb") as f:
                f.write(self.written)
        finally:
            with self.parallel_lock:
                self.parallel -= 1
        if progress_cb is not None:
            progress_cb(len(self.written), len(self.written))
        return len(self.written)


class SmokeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.calibre = FakeCalibre()
        self.api = FakeAPI(self.base, self.calibre)
        self.client = FakeClient()
        self.tool = ZlibraryTool()
        self.tool.api = self.api
        self.tool.add_msg = lambda user_id, status, msg: self.api.msgs.append(
            (status, msg))
        self.tool.get_client = lambda config=None: self.client
        ZlibraryTool._jobs = {}
        ZlibraryTool._cancel_events = {}
        ZlibraryTool._job_meta = {}
        ZlibraryTool._search_cache = {}
        ZlibraryTool._client = None
        ZlibraryTool._client_key = None

    def add_book(self, token="a" * 16):
        books = [{
            "id": "123", "hash": "abcdef", "title": "Test Book",
            "author": "Test Author", "extension": "epub", "size": "100 KB",
            "download": "/dl/token", "href": "/book/123/x.html",
            "origin": "https://origin.example",
        }]
        with ZlibraryTool._search_lock:
            ZlibraryTool._search_cache[token] = (books[0], 10 ** 12)
        return token

    def start_job(self, token, job_id="job1", title="Test Book"):
        self.assertTrue(ZlibraryTool._begin(job_id, token, title))
        return job_id

    def use_credentials_key(self):
        key_path = os.path.join(self.base, "credentials.key")
        self.tool._credential_key_path = lambda: key_path
        return key_path


class ConfigSmoke(SmokeBase):
    def test_default_tag_is_empty(self):
        self.assertEqual(self.tool.load_config()["tag"], "")

    def test_roundtrip_and_normalization(self):
        self.api._config = {"accounts": [pool.new_account("A")]}
        saved = self.tool.save_config({
            "entry_url": "zlib.ch",
            "dns_mode": "weird",
            "timeout": "999",
            "max_mb": "0",
            "tag": "  Z  ",
            "guest_enabled": False,
            "max_concurrent": "99",
        })
        self.assertEqual(saved["entry_url"], "https://zlib.ch/")
        self.assertEqual(saved["dns_mode"], "auto")
        self.assertEqual(saved["timeout"], 120)
        self.assertEqual(saved["max_mb"], 1)
        self.assertEqual(saved["max_concurrent"], 8)
        self.assertEqual(saved["tag"], "Z")
        self.assertFalse(saved["guest_enabled"])
        # accounts 不在 config patch 里，保存设置不能清掉号池
        self.assertEqual(len(self.api._config["accounts"]), 1)

    def test_public_config_masks_accounts(self):
        account = pool.new_account("A", remix_userid="1001",
                                   remix_userkey="supersecretkey", bsrv="bsrvsecret")
        self.api._config = {"accounts": [account]}
        data = self.tool.public_config()
        self.assertEqual(data["accounts"][0]["remix_userkey"],
                         "****" + "supersecretkey"[-4:])
        self.assertNotIn("supersecretkey", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("bsrvsecret", json.dumps(data, ensure_ascii=False))

    def test_add_account_rejects_over_pool_limit(self):
        accounts = [pool.new_account("A%d" % index, remix_userid=str(index),
                                     remix_userkey="key")
                    for index in range(tool_mod._MAX_ACCOUNTS)]
        self.api._config = {"accounts": accounts, "guest_enabled": False}
        with self.assertRaises(zlib_client.ZlibError) as ctx:
            self.tool.account_action({
                "action": "add", "mode": "cookie",
                "remix_userid": "new", "remix_userkey": "key",
            })
        self.assertEqual(ctx.exception.code, "accounts.limit")
        self.assertEqual(len(self.api._config["accounts"]),
                         tool_mod._MAX_ACCOUNTS)


class SearchSmoke(SmokeBase):
    def test_search_maps_iso_language_codes_to_search_names(self):
        captured = {}

        def fake_search(origin, query, page, limit, filters=None, timeout=None):
            captured["filters"] = filters
            return {"books": [], "pagination": {}, "source": "eapi"}

        self.client.eapi_search = fake_search
        self.tool.search_sync("q", 1, {
            "languages": ["zh", "zh-Hant", "ja"],
        }, 30)
        self.assertEqual(
            captured["filters"]["languages"], ["chinese", "japanese"])

    def test_search_returns_opaque_token_and_caches_book(self):
        data = self.tool.search_sync("q", 1, {}, 30)
        self.assertEqual(len(data["books"]), 1)
        public = data["books"][0]
        self.assertIn("book_token", public)
        self.assertNotIn("download", public)
        self.assertNotIn("hash", public)
        book = self.tool.load_book(public["book_token"])
        self.assertEqual(book["id"], "123")
        self.assertEqual(book["download"], "/dl/token")

    def test_load_book_rejects_bad_or_expired_token(self):
        with self.assertRaises(zlib_client.ZlibError) as ctx:
            self.tool.load_book("../bad")
        self.assertEqual(ctx.exception.code, "params.invalid")


class DownloadSmoke(SmokeBase):
    def test_download_import_and_tag(self):
        token = self.add_book()
        account = pool.new_account("A", remix_userid="1001",
                                   remix_userkey="key-abcdef")
        self.api._config = {"accounts": [account], "tag": "Z-Library",
                            "guest_enabled": False}
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token, job_id)
        snap = ZlibraryTool.snapshot(job_id)
        self.assertEqual(snap["status"], "completed")
        self.assertEqual(snap["book_id"], 1)
        self.assertEqual(self.calibre.imported[0][1], "Test Book")
        self.assertEqual(self.calibre.imported[0][2], ["Test Author"])
        self.assertEqual(self.calibre.set_calls, [1])
        self.assertEqual(self.calibre.meta[1].tags, ["Z-Library"])
        self.assertEqual(self.api.completed, [None])
        self.assertFalse(os.path.exists(work_dir))

    def test_success_download_sends_persistent_message(self):
        token = self.add_book()
        self.api._config = {"guest_enabled": True}
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token, job_id)
        self.assertEqual(ZlibraryTool.snapshot(job_id)["status"], "completed")
        self.assertIn(
            ("success", "Z-Library 入库成功：Test Book"), self.api.msgs)

    def test_message_failure_does_not_fail_completed_download(self):
        token = self.add_book()
        self.api._config = {"guest_enabled": True}
        self.tool.add_msg = mock.Mock(side_effect=RuntimeError("no message"))
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token, job_id)
        self.assertEqual(ZlibraryTool.snapshot(job_id)["status"], "completed")

    def test_task_progress_reaches_host(self):
        token = self.add_book()
        self.api._config = {"guest_enabled": True}
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token, job_id)
        updates = self.api.progress_updates
        self.assertTrue(all(task_id == 1 for task_id, _p, _d in updates))
        percents = [percent for _task, percent, _data in updates]
        self.assertIn(3, percents)
        self.assertIn(96, percents)
        self.assertIn(100, percents)
        self.assertEqual(updates[-1][2].get("stage"), "done")

    def test_long_title_download_keeps_metadata_but_short_filename(self):
        token = self.add_book()
        long_title = "中" * 1000
        with ZlibraryTool._search_lock:
            book, expires = ZlibraryTool._search_cache[token]
            book = dict(book, title=long_title)
            ZlibraryTool._search_cache[token] = (book, expires)
        self.api._config = {"guest_enabled": True}
        job_id = self.start_job(token, title=long_title)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token, job_id)
        self.assertEqual(ZlibraryTool.snapshot(job_id)["status"], "completed")
        self.assertEqual(self.calibre.imported[0][1], long_title)
        filename = os.path.basename(self.client.last_dest)
        self.assertLessEqual(len(filename.encode("utf-8")), 209)

    def test_cancel_mid_download(self):
        token = self.add_book()
        self.api._config = {"guest_enabled": True}
        self.client.cancel_on_stream = True
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token, job_id)
        snap = ZlibraryTool.snapshot(job_id)
        self.assertEqual(snap["status"], "cancelled")
        self.assertEqual(self.calibre.imported, [])
        self.assertEqual(self.api.completed, ["已取消"])

    def test_cancel_survives_message_failure(self):
        token = self.add_book()
        self.api._config = {"guest_enabled": True}
        self.client.cancel_on_stream = True
        self.tool.add_msg = mock.Mock(side_effect=RuntimeError("no message"))
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token, job_id)
        self.assertEqual(ZlibraryTool.snapshot(job_id)["status"], "cancelled")

    def test_success_survives_post_import_config_write_failure(self):
        token = self.add_book()
        account = pool.new_account("A", remix_userid="1001",
                                   remix_userkey="key-abcdef")
        self.api._config = {"accounts": [account], "guest_enabled": False}
        original_set_config = self.api.set_config

        def flaky_set_config(data):
            if self.calibre.imported:
                raise RuntimeError("disk full")
            original_set_config(data)

        self.api.set_config = flaky_set_config
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token, job_id)
        self.assertEqual(ZlibraryTool.snapshot(job_id)["status"], "completed")

    def test_download_failure_marks_failed(self):
        token = self.add_book()
        self.api._config = {"guest_enabled": True}
        self.client.stream_error = zlib_client.ZlibError("download.too_large", "boom")
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token, job_id)
        snap = ZlibraryTool.snapshot(job_id)
        self.assertEqual(snap["status"], "failed")
        self.assertEqual(snap["error"], "download.too_large")
        self.assertIn("danger", [m[0] for m in self.api.msgs])
        self.assertFalse(os.path.exists(work_dir))

    def test_concurrent_jobs_do_not_overwrite_each_other(self):
        token = self.add_book()
        self.api._config = {"guest_enabled": True}
        job_ids = []
        for index in range(3):
            job_id = self.start_job(token, "job%d" % index, "Book %d" % index)
            job_ids.append(job_id)
        self.assertEqual(ZlibraryTool.queued_count(), 3)
        self.assertEqual(ZlibraryTool.active_count(), 0)
        self.assertEqual(len(ZlibraryTool.snapshot_jobs()), 3)
        for job_id in job_ids[:2]:
            work_dir = self.tool.new_work_dir(3600)[1]
            self.tool.run_download(7, work_dir, token, job_id)
            self.assertEqual(ZlibraryTool.snapshot(job_id)["status"], "completed")
        self.assertEqual(ZlibraryTool.snapshot(job_ids[0])["book_id"], 1)
        self.assertEqual(ZlibraryTool.snapshot(job_ids[1])["book_id"], 2)
        # 离线测试的 _begin 没带宿主 user_id，补位时会按 missing context 标失败。
        self.assertEqual(ZlibraryTool.snapshot(job_ids[2])["status"], "failed")
        self.assertEqual(ZlibraryTool.queued_count(), 0)

    def test_queue_dispatches_up_to_configured_concurrency(self):
        token = self.add_book()
        self.api._config = {"guest_enabled": True, "max_concurrent": 2}
        job_ids = []
        for index in range(3):
            job_id = "q%d" % index
            work_dir = self.tool.new_work_dir(3600)[1]
            self.assertTrue(ZlibraryTool._begin(
                job_id, token, "Queued %d" % index,
                user_id=7, work_dir=work_dir))
            job_ids.append(job_id)
        self.assertEqual(ZlibraryTool.queued_count(), 3)
        starts = []

        def fake_run(tool_self, user_id, work_dir, book_token, job_id,
                     account_id=None, book_snapshot=None):
            starts.append(job_id)

        with mock.patch.object(ZlibraryTool, "run_download", fake_run):
            self.tool._dispatch_jobs()
            self.assertEqual(starts, ["q0", "q1"])
            positions = {
                job["job_id"]: job.get("queue_position")
                for job in ZlibraryTool.snapshot_jobs()
            }
            self.assertEqual(positions[job_ids[2]], 1)
            ZlibraryTool._update(job_ids[0], status="completed")
            self.tool._dispatch_jobs()
        self.assertEqual(starts, ["q0", "q1", "q2"])
        self.assertEqual(ZlibraryTool.queued_count(), 0)

    def test_account_added_during_download_is_preserved(self):
        token = self.add_book()
        account = pool.new_account("A", remix_userid="1001",
                                   remix_userkey="key-abcdef")
        self.api._config = {"accounts": [account], "guest_enabled": False}

        def add_second_account():
            self.tool.account_action({
                "action": "add", "mode": "cookie", "label": "B",
                "remix_userid": "2002", "remix_userkey": "cookie-key-xyz",
            })

        self.client.on_stream = add_second_account
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token, job_id)
        self.assertEqual(ZlibraryTool.snapshot(job_id)["status"], "completed")
        labels = [item["label"] for item in self.api._config["accounts"]]
        self.assertEqual(labels, ["A", "B"])

    def test_account_deleted_during_download_is_not_resurrected(self):
        token = self.add_book()
        account = pool.new_account("A", remix_userid="1001",
                                   remix_userkey="key-abcdef")
        self.api._config = {"accounts": [account], "guest_enabled": False}

        def delete_account():
            self.tool.account_action({"action": "delete",
                                      "id": account["id"]})

        self.client.on_stream = delete_account
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token, job_id)
        self.assertEqual(ZlibraryTool.snapshot(job_id)["status"], "completed")
        self.assertEqual(self.api._config["accounts"], [])

    def test_invalid_profile_status_is_persisted(self):
        token = self.add_book()
        account = pool.new_account("A", remix_userid="1001",
                                   remix_userkey="key-abcdef")
        self.api._config = {"accounts": [account], "guest_enabled": True}
        self.client.profile_error = zlib_client.ZlibError(
            "accounts.session_expired", "expired")
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token, job_id)
        self.assertEqual(ZlibraryTool.snapshot(job_id)["status"], "completed")
        stored = self.api._config["accounts"][0]
        self.assertEqual(stored["status"], pool.STATUS_INVALID)

    def test_thread_start_failure_fails_job_and_cleans_workdir(self):
        token = self.add_book()
        self.api._config = {"guest_enabled": True}
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]

        class BrokenThread(object):
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("cannot start thread")

        service_cls = tool_mod.AsyncService
        service_cls.async_mode = staticmethod(lambda: True)
        original_thread = threading.Thread
        threading.Thread = BrokenThread
        try:
            self.tool.run_download(7, work_dir, token, job_id)
        finally:
            threading.Thread = original_thread
            del service_cls.async_mode
        self.assertEqual(ZlibraryTool.snapshot(job_id)["status"], "failed")
        self.assertFalse(os.path.exists(work_dir))

    def test_concurrent_jobs_pick_different_accounts(self):
        token = self.add_book()
        first = pool.new_account("A", remix_userid="1001",
                                 remix_userkey="key-a")
        second = pool.new_account("B", remix_userid="2002",
                                  remix_userkey="key-b")
        self.api._config = {"accounts": [first, second], "guest_enabled": False}
        job_ids = [self.start_job(token, "acc%d" % index) for index in range(2)]
        self.client.barrier = threading.Barrier(2)
        service_cls = tool_mod.AsyncService
        service_cls.async_mode = staticmethod(lambda: True)
        try:
            for job_id in job_ids:
                work_dir = self.tool.new_work_dir(3600)[1]
                self.tool.run_download(7, work_dir, token, job_id)
            deadline = time.time() + 5
            while time.time() < deadline:
                statuses = [ZlibraryTool.snapshot(job_id)["status"]
                            for job_id in job_ids]
                if all(status in ("completed", "failed", "cancelled")
                       for status in statuses):
                    break
                time.sleep(0.02)
        finally:
            del service_cls.async_mode
        used = {account_id for _book, account_id in self.client.prepared}
        self.assertEqual(used, {first["id"], second["id"]})
        self.assertEqual([ZlibraryTool.snapshot(job_id)["status"]
                          for job_id in job_ids],
                         ["completed", "completed"])

    def test_downloads_run_in_parallel_threads(self):
        token = self.add_book()
        self.api._config = {"guest_enabled": True}
        job_ids = [self.start_job(token, "par%d" % index) for index in range(2)]
        self.client.barrier = threading.Barrier(2)
        service_cls = tool_mod.AsyncService
        service_cls.async_mode = staticmethod(lambda: True)
        try:
            for job_id in job_ids:
                work_dir = self.tool.new_work_dir(3600)[1]
                self.tool.run_download(7, work_dir, token, job_id)
            deadline = time.time() + 5
            while time.time() < deadline:
                statuses = [ZlibraryTool.snapshot(job_id)["status"]
                            for job_id in job_ids]
                if all(status in ("completed", "failed", "cancelled")
                       for status in statuses):
                    break
                time.sleep(0.02)
        finally:
            del service_cls.async_mode
        self.assertEqual(self.client.max_parallel, 2)
        self.assertEqual([ZlibraryTool.snapshot(job_id)["status"]
                          for job_id in job_ids],
                         ["completed", "completed"])


class AccountSmoke(SmokeBase):
    def test_refresh_action_updates_server_quota(self):
        account = pool.new_account("A", remix_userid="1001",
                                   remix_userkey="key-abcdef")
        self.api._config = {"accounts": [account], "guest_enabled": True}
        result = self.tool.account_action({"action": "refresh"})
        self.assertEqual(result["accounts"][0]["quota_today"], 0)
        self.assertEqual(result["accounts"][0]["status"], pool.STATUS_OK)

    def test_refresh_entry_failure_marks_all_accounts_stale_once(self):
        first = pool.new_account("A", remix_userid="1001",
                                 remix_userkey="key-abcdef")
        second = pool.new_account("B", remix_userid="2002",
                                  remix_userkey="key-abcdef")
        self.api._config = {"accounts": [first, second], "guest_enabled": True}

        def fail_origin():
            raise zlib_client.ZlibError(
                "entry.http", "HTTP 504 at https://mirror.example/")

        self.client.origin = fail_origin
        with mock.patch.object(tool_mod.logging, "warning") as warning:
            result = self.tool.account_action({"action": "refresh"})
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["failed"], 2)
        # 入口只解析一次：没有进入任何账号的 EAPI profile。
        self.assertEqual(self.client.profile_calls, 0)
        self.assertEqual(warning.call_count, 1)
        self.assertIn("entry.http", str(warning.call_args))
        for account in self.api._config["accounts"]:
            self.assertTrue(account["quota_stale"])

    def test_add_password_account_never_persists_password(self):
        result = self.tool.account_action({
            "action": "add", "mode": "password", "label": "A",
            "email": "user@example.invalid", "password": "secret-password",
        })
        self.assertEqual(len(result["accounts"]), 1)
        stored = json.dumps(self.api._config, ensure_ascii=False)
        self.assertNotIn("secret-password", stored)
        self.assertIn("key-abcdef", stored)
        public = result["accounts"][0]
        self.assertNotIn("key-abcdef", json.dumps(public, ensure_ascii=False))
        self.assertEqual(public["status"], "ok")
        self.assertEqual(public["remaining"], 10)

    def test_add_cookie_account_and_mask(self):
        result = self.tool.account_action({
            "action": "add", "mode": "cookie", "label": "B",
            "remix_userid": "2002", "remix_userkey": "cookie-key-xyz",
        })
        public = result["accounts"][0]
        self.assertEqual(public["remix_userkey"], "****-xyz")
        self.assertNotIn("cookie-key-xyz", json.dumps(public, ensure_ascii=False))

    def test_test_and_delete_account(self):
        self.tool.account_action({
            "action": "add", "mode": "cookie", "label": "C",
            "remix_userid": "3003", "remix_userkey": "key-abcdef",
        })
        account_id = self.api._config["accounts"][0]["id"]
        result = self.tool.account_action({"action": "test", "id": account_id})
        self.assertEqual(result["account"]["status"], "ok")
        result = self.tool.account_action({"action": "delete", "id": account_id})
        self.assertEqual(result["accounts"], [])

    def test_update_login_keeps_identity_and_stores_password(self):
        key_path = self.use_credentials_key()
        self.tool.account_action({
            "action": "add", "mode": "cookie", "label": "C",
            "remix_userid": "3003", "remix_userkey": "old-key-abcd",
        })
        account_id = self.api._config["accounts"][0]["id"]
        result = self.tool.account_action({
            "action": "update_login", "id": account_id,
            "email": "user@example.invalid", "password": "secret-password",
        })
        self.assertEqual(result["account"]["id"], account_id)
        self.assertEqual(result["account"]["label"], "C")
        self.assertEqual(result["account"]["status"], pool.STATUS_OK)
        self.assertTrue(result["account"]["has_password"])
        self.assertTrue(result["account"]["auto_renew"])
        stored = self.api._config["accounts"][0]
        self.assertEqual(stored["remix_userid"], "1001")
        self.assertEqual(stored["remix_userkey"], "key-abcdef")
        self.assertEqual(stored["email"], "user@example.invalid")
        self.assertEqual(
            tool_mod.credential_store.decrypt_password(
                stored["password_enc"], key_path),
            "secret-password")

    def test_update_login_requires_email_and_password(self):
        self.tool.account_action({
            "action": "add", "mode": "cookie", "label": "C",
            "remix_userid": "3003", "remix_userkey": "old-key-abcd",
        })
        account_id = self.api._config["accounts"][0]["id"]
        with self.assertRaises(zlib_client.ZlibError) as ctx:
            self.tool.account_action({
                "action": "update_login", "id": account_id,
                "email": "user@example.invalid", "password": "",
            })
        self.assertEqual(ctx.exception.code, "params.missing")
        self.assertEqual(
            self.api._config["accounts"][0]["remix_userkey"], "old-key-abcd")

    def test_invalid_update_login_does_not_replace_old_session(self):
        self.tool.account_action({
            "action": "add", "mode": "cookie", "label": "C",
            "remix_userid": "3003", "remix_userkey": "old-key-abcd",
        })
        account_id = self.api._config["accounts"][0]["id"]
        with self.assertRaises(zlib_client.ZlibError) as ctx:
            self.tool.account_action({
                "action": "update_login", "id": account_id,
                "email": "user@example.invalid", "password": "wrong-password",
            })
        self.assertEqual(ctx.exception.code, "accounts.login_failed")
        stored = self.api._config["accounts"][0]
        self.assertEqual(stored["remix_userkey"], "old-key-abcd")
        self.assertEqual(stored["status"], pool.STATUS_OK)

    def test_add_password_can_store_encrypted_password(self):
        key_path = self.use_credentials_key()
        result = self.tool.account_action({
            "action": "add", "mode": "password", "label": "A",
            "email": "user@example.invalid", "password": "secret-password",
            "save_password": True,
        })
        stored = self.api._config["accounts"][0]
        self.assertTrue(stored["auto_renew"])
        self.assertTrue(stored["password_enc"].startswith("v1:"))
        self.assertNotIn("secret-password", json.dumps(
            self.api._config, ensure_ascii=False))
        public = result["accounts"][0]
        self.assertTrue(public["has_password"])
        self.assertTrue(public["auto_renew"])
        self.assertNotIn("password_enc", json.dumps(public, ensure_ascii=False))
        self.assertEqual(
            tool_mod.credential_store.decrypt_password(
                stored["password_enc"], key_path),
            "secret-password")

    def test_expired_session_auto_renews_on_test(self):
        self.use_credentials_key()
        self.tool.account_action({
            "action": "add", "mode": "password", "label": "A",
            "email": "user@example.invalid", "password": "secret-password",
            "save_password": True,
        })
        account_id = self.api._config["accounts"][0]["id"]
        self.client.profile_error = zlib_client.ZlibError(
            "accounts.session_expired", "expired")
        result = self.tool.account_action({"action": "test", "id": account_id})
        self.assertEqual(result["account"]["status"], pool.STATUS_OK)
        self.assertEqual(self.client.login_calls, 2)
        stored = self.api._config["accounts"][0]
        self.assertEqual(stored["remix_userkey"], "key-abcdef")
        self.assertEqual(stored["status"], pool.STATUS_OK)

    def test_refresh_action_auto_renews_expired_session(self):
        self.use_credentials_key()
        self.tool.account_action({
            "action": "add", "mode": "password", "label": "A",
            "email": "user@example.invalid", "password": "secret-password",
            "save_password": True,
        })
        self.client.profile_error = zlib_client.ZlibError(
            "accounts.session_expired", "expired")
        result = self.tool.account_action({"action": "refresh"})
        self.assertEqual(result["accounts"][0]["status"], pool.STATUS_OK)
        stored = self.api._config["accounts"][0]
        self.assertEqual(stored["remix_userkey"], "key-abcdef")
        self.assertEqual(stored["status"], pool.STATUS_OK)

    def test_download_retries_after_auto_renewing_session(self):
        self.use_credentials_key()
        self.tool.account_action({
            "action": "add", "mode": "password", "label": "A",
            "email": "user@example.invalid", "password": "secret-password",
            "save_password": True,
        })
        account_id = self.api._config["accounts"][0]["id"]
        self.client.prepare_errors = [zlib_client.ZlibError(
            "accounts.session_expired", "expired")]
        token = self.add_book()
        job_id = self.start_job(token)
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(
            7, work_dir, token, job_id, account_id=account_id)
        self.assertEqual(ZlibraryTool.snapshot(job_id)["status"], "completed")
        self.assertEqual(len(self.client.prepared), 2)
        stored = self.api._config["accounts"][0]
        self.assertEqual(stored["remix_userkey"], "key-abcdef")
        self.assertEqual(stored["status"], pool.STATUS_OK)

    def test_failed_auto_renew_enters_cooldown(self):
        self.use_credentials_key()
        self.tool.account_action({
            "action": "add", "mode": "password", "label": "A",
            "email": "user@example.invalid", "password": "secret-password",
            "save_password": True,
        })
        account_id = self.api._config["accounts"][0]["id"]
        self.client.profile_error = zlib_client.ZlibError(
            "accounts.session_expired", "expired")
        self.client.login_error = zlib_client.ZlibError(
            "accounts.login_failed", "bad password")
        result = self.tool.account_action({"action": "test", "id": account_id})
        self.assertEqual(result["account"]["status"], pool.STATUS_INVALID)
        self.assertGreater(self.api._config["accounts"][0]["cooldown_until"], 0)


class CredentialStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.key_path = os.path.join(self._tmp.name, "credentials.key")

    def test_roundtrip_and_ciphertext(self):
        token = tool_mod.credential_store.encrypt_password(
            "secret-password", self.key_path)
        self.assertTrue(token.startswith("v1:"))
        self.assertNotIn("secret-password", token)
        self.assertEqual(
            tool_mod.credential_store.decrypt_password(token, self.key_path),
            "secret-password")

    def test_tampered_ciphertext_is_rejected(self):
        token = tool_mod.credential_store.encrypt_password(
            "secret-password", self.key_path)
        prefix, payload = token.split(":", 1)
        raw = bytearray(base64.urlsafe_b64decode(
            payload + "=" * (-len(payload) % 4)))
        raw[-1] ^= 1
        tampered = prefix + ":" + base64.urlsafe_b64encode(
            bytes(raw)).decode("ascii").rstrip("=")
        with self.assertRaises(tool_mod.credential_store.CredentialStoreError):
            tool_mod.credential_store.decrypt_password(tampered, self.key_path)


class RotationTest(SmokeBase):
    def _account(self, label, user_id):
        return pool.new_account(label, remix_userid=str(user_id),
                                remix_userkey="key-" + label)

    def test_round_robin_alternates_and_persists_cursor(self):
        first = self._account("A", 1001)
        second = self._account("B", 1002)
        self.api._config = {
            "accounts": [first, second], "rotation": "round_robin",
            "guest_enabled": False,
        }
        book = {"id": "123", "title": "Test Book", "extension": "epub"}
        config = self.tool.load_config()
        _target, used_first = self.tool._pick_download(
            self.client, "https://origin.example", book, config)
        self.assertEqual(used_first["id"], first["id"])
        self.assertEqual(
            self.api._config.get("rotation_cursor"), first["id"])
        _target, used_second = self.tool._pick_download(
            self.client, "https://origin.example", book,
            self.tool.load_config())
        self.assertEqual(used_second["id"], second["id"])
        self.assertEqual(
            self.api._config.get("rotation_cursor"), second["id"])

    def test_round_robin_skips_disabled_and_wraps(self):
        first = self._account("A", 1001)
        second = self._account("B", 1002)
        first["enabled"] = False
        self.assertEqual(
            [item["id"] for item in pool.candidate_accounts(
                [first, second], rotation="round_robin", now=200.0)],
            [second["id"]])
        cursor = second["id"]
        self.assertEqual(
            [item["id"] for item in pool.candidate_accounts(
                [first, second], rotation="round_robin", cursor=cursor,
                now=200.0)],
            [second["id"]])

    def test_least_used_keeps_healthy_account_ahead_of_error(self):
        error = self._account("error", 1001)
        healthy = self._account("healthy", 1002)
        error.update(status=pool.STATUS_ERROR, last_used=0, cooldown_until=0)
        healthy.update(status=pool.STATUS_OK, last_used=100)
        candidates = pool.candidate_accounts(
            [error, healthy], rotation="least_used", now=200.0)
        self.assertEqual(candidates[0]["id"], healthy["id"])

    def test_create_task_label_contains_book_title(self):
        task = mock.Mock()
        task.id = 9
        with mock.patch.object(
                tool_mod.BackgroundService, "update_task",
                return_value=task) as update:
            task_id = self.tool.create_task({"book_title": "A Book"})
        self.assertEqual(task_id, 9)
        self.assertIn("A Book", update.call_args[1]["service_item"])

    def test_persist_keeps_newer_last_used_from_other_worker(self):
        fresh = self._account("fresh", 1001)
        fresh["last_used"] = 100
        fresh["quota_updated_at"] = 100
        stale = pool.normalize_account(fresh)
        stale["last_used"] = 0
        stale["quota_updated_at"] = 200
        self.api._config = {"accounts": [fresh]}
        self.tool._persist_account_state(stale, only_if_newer=True)
        self.assertEqual(self.api._config["accounts"][0]["last_used"], 100)

    def test_persist_keeps_newer_status_over_stale_snapshot(self):
        fresh = self._account("fresh", 1001)
        fresh.update(status=pool.STATUS_OK, cooldown_until=0,
                     state_updated_at=200)
        stale = pool.normalize_account(fresh)
        stale.update(status=pool.STATUS_ERROR, cooldown_until=999,
                     state_updated_at=100)
        self.api._config = {"accounts": [fresh]}
        self.tool._persist_account_state(stale, only_if_newer=True)
        saved = self.api._config["accounts"][0]
        self.assertEqual(saved["status"], pool.STATUS_OK)
        self.assertEqual(saved["cooldown_until"], 0)

    def test_persist_does_not_rollback_new_quota_state(self):
        fresh = self._account("fresh", 1001)
        fresh.update(state_updated_at=200, quota_stale=False,
                     quota_stale_at=200)
        stale = pool.normalize_account(fresh)
        stale.update(state_updated_at=100, quota_stale=True,
                     quota_stale_at=100)
        self.api._config = {"accounts": [fresh]}
        self.tool._persist_account_state(stale, only_if_newer=True)
        self.assertFalse(self.api._config["accounts"][0]["quota_stale"])

    def test_persist_does_not_rollback_new_email(self):
        fresh = self._account("fresh", 1001)
        fresh.update(email="new@example.com", state_updated_at=200)
        stale = pool.normalize_account(fresh)
        stale.update(email="old@example.com", state_updated_at=100)
        self.api._config = {"accounts": [fresh]}
        self.tool._persist_account_state(stale, only_if_newer=True)
        self.assertEqual(
            self.api._config["accounts"][0]["email"], "new@example.com")

    def test_credentials_refuse_stale_identity_write(self):
        fresh = self._account("fresh", 1001)
        fresh.update(email="new@example.com", password_enc="v1:new",
                     remix_userkey="new-key")
        self.api._config = {"accounts": [fresh]}
        stale = pool.normalize_account(fresh)
        stale.update(email="old@example.com", password_enc="v1:old",
                     remix_userkey="old-key")
        expected = {key: stale.get(key)
                    for key in tool_mod._ACCOUNT_IDENTITY_FIELDS}
        self.tool._persist_account_credentials(stale, expected=expected)
        saved = self.api._config["accounts"][0]
        self.assertEqual(saved["email"], "new@example.com")
        self.assertEqual(saved["password_enc"], "v1:new")
        self.assertEqual(saved["remix_userkey"], "new-key")


class AccountRefreshTest(SmokeBase):
    def test_refresh_includes_disabled_accounts_and_reports_failures(self):
        account = pool.new_account(
            "A", remix_userid="1001", remix_userkey="key-abcdef")
        account["enabled"] = False
        self.api._config = {"accounts": [account], "guest_enabled": False}
        self.client.profile_error = zlib_client.ZlibError("eapi.http", "down")
        result = self.tool.account_action({"action": "refresh"})
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.client.profile_calls, 1)
        self.assertTrue(self.api._config["accounts"][0]["quota_stale"])

    def test_refresh_respects_renewal_cooldown(self):
        account = pool.new_account(
            "A", remix_userid="1001", remix_userkey="key-abcdef",
            password_enc="v1:secret", auto_renew=True)
        account["status"] = pool.STATUS_INVALID
        account["cooldown_until"] = time.time() + 600
        self.api._config = {"accounts": [account], "guest_enabled": False}
        result = self.tool.account_action({"action": "refresh"})
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.client.profile_calls, 0)
        self.assertEqual(self.client.login_calls, 0)
        self.assertTrue(self.api._config["accounts"][0]["quota_stale"])


class AccountQuotaTest(unittest.TestCase):
    def test_invalid_auto_renew_retries_after_cooldown(self):
        account = pool.new_account(
            "A", remix_userid="1", remix_userkey="k",
            password_enc="v1:secret", auto_renew=True)
        now = 1000.0
        account["status"] = pool.STATUS_INVALID
        account["cooldown_until"] = now + pool.INVALID_RECHECK_SECONDS
        self.assertFalse(pool.is_selectable(account, now=now + 1))
        self.assertTrue(pool.is_selectable(
            account, now=now + pool.INVALID_RECHECK_SECONDS + 1))
        account["auto_renew"] = False
        self.assertFalse(pool.is_selectable(
            account, now=now + pool.INVALID_RECHECK_SECONDS + 1))

    def test_exhausted_recovers_after_cooldown(self):
        account = pool.new_account("A", remix_userid="1", remix_userkey="k")
        now = 1000.0
        pool.apply_profile(account, {"quota_today": 10, "quota_limit": 10},
                           now=now)
        self.assertEqual(account["status"], pool.STATUS_EXHAUSTED)
        self.assertFalse(pool.is_selectable(account, now=now + 1))
        self.assertTrue(pool.is_selectable(
            account, now=now + pool.EXHAUSTED_RECHECK_SECONDS + 1))
        pool.apply_profile(account, {"quota_today": 9, "quota_limit": 10},
                           now=now + pool.EXHAUSTED_RECHECK_SECONDS + 1)
        self.assertEqual(account["status"], pool.STATUS_OK)

    def test_success_does_not_increment_local_quota(self):
        account = pool.new_account("A", remix_userid="1", remix_userkey="k")
        account["quota_today"] = 5
        pool.mark_success(account, now=1000.0)
        self.assertEqual(account["quota_today"], 5)

    def test_select_prefers_least_recently_used_ok_account(self):
        used = pool.new_account("used", remix_userid="1", remix_userkey="a")
        fresh = pool.new_account("fresh", remix_userid="2", remix_userkey="b")
        used.update(status=pool.STATUS_OK, last_used=100.0,
                    quota_today=1, quota_limit=10)
        fresh.update(status=pool.STATUS_UNKNOWN, last_used=0,
                     quota_today=None, quota_limit=None)
        self.assertIs(pool.select_account([used, fresh], now=200.0), fresh)


if __name__ == "__main__":
    unittest.main(verbosity=2)
