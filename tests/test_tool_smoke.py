# -*- coding: utf-8 -*-
"""ZlibraryTool 冒烟：stub 宿主 + 假客户端，离线跑通设置、搜索缓存、号池、
下载→入库→补标签、取消与失败路径。"""
import importlib
import json
import os
import sys
import tempfile
import types
import unittest

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
    toolbox = types.ModuleType("webserver.toolbox")
    base_tool = types.ModuleType("webserver.toolbox.base_tool")

    class BaseTool(object):
        SUPPORTED_FORMATS = {"epub", "pdf", "azw3", "mobi", "txt"}

        def __init__(self):
            self.api = None

        def add_msg(self, user_id, status, msg):
            pass

    base_tool.BaseTool = BaseTool
    webserver.handlers = handlers
    handlers.base = handlers_base
    webserver.i18n = i18n
    webserver.services = services
    webserver.toolbox = toolbox
    toolbox.base_tool = base_tool
    for name, mod in (("webserver", webserver), ("webserver.handlers", handlers),
                      ("webserver.handlers.base", handlers_base),
                      ("webserver.i18n", i18n), ("webserver.services", services),
                      ("webserver.toolbox", toolbox),
                      ("webserver.toolbox.base_tool", base_tool)):
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
        return 1

    def update_progress(self, task_id, progress, progress_data=None):
        pass

    def complete_task(self, task_id, error_message=None):
        self.completed.append(error_message)

    def send_message(self, user_id, msg, status="info"):
        self.msgs.append((status, msg))


class FakeClient(object):
    def __init__(self):
        self.profile_calls = 0
        self.prepared = []
        self.stream_error = None
        self.cancel_on_stream = False
        self.written = b"PK\x03\x04fake-epub"

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
        return {"quota_today": 0, "quota_limit": 10, "remaining": 10,
                "email": account.get("email", ""), "premium": False}

    def eapi_login(self, origin, email, password, timeout=None):
        if password != "secret-password":
            raise zlib_client.ZlibError("accounts.login_failed", "bad")
        return {"remix_userid": "1001", "remix_userkey": "key-abcdef",
                "cookies": {"remix_userid": "1001", "remix_userkey": "key-abcdef"}}

    def html_login(self, origin, email, password, timeout=None):
        raise zlib_client.ZlibError("accounts.login_failed", "bad")

    def prepare_download(self, origin, book, account=None, timeout=None):
        self.prepared.append((book.get("id"), bool(account)))
        return {"url": "https://cdn.example/book.epub"}

    def stream_download(self, target, dest, *, max_bytes, cancel_event=None,
                        progress_cb=None, timeout=None):
        if self.stream_error is not None:
            raise self.stream_error
        if self.cancel_on_stream:
            cancel_event.set()
            return 0
        with open(dest, "wb") as f:
            f.write(self.written)
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
        ZlibraryTool._state = {}
        ZlibraryTool._search_cache = {}
        ZlibraryTool._cancel_event = None
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
        })
        self.assertEqual(saved["entry_url"], "https://zlib.ch/")
        self.assertEqual(saved["dns_mode"], "auto")
        self.assertEqual(saved["timeout"], 120)
        self.assertEqual(saved["max_mb"], 1)
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


class SearchSmoke(SmokeBase):
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
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token)
        snap = ZlibraryTool.snapshot()
        self.assertEqual(snap["status"], "completed")
        self.assertEqual(snap["book_id"], 1)
        self.assertEqual(self.calibre.imported[0][1], "Test Book")
        self.assertEqual(self.calibre.imported[0][2], ["Test Author"])
        self.assertEqual(self.calibre.set_calls, [1])
        self.assertEqual(self.calibre.meta[1].tags, ["Z-Library"])
        self.assertEqual(self.api.completed, [None])
        self.assertFalse(os.path.exists(work_dir))

    def test_cancel_mid_download(self):
        token = self.add_book()
        self.api._config = {"guest_enabled": True}
        self.client.cancel_on_stream = True
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token)
        snap = ZlibraryTool.snapshot()
        self.assertEqual(snap["status"], "cancelled")
        self.assertEqual(self.calibre.imported, [])
        self.assertEqual(self.api.completed, ["已取消"])

    def test_download_failure_marks_failed(self):
        token = self.add_book()
        self.api._config = {"guest_enabled": True}
        self.client.stream_error = zlib_client.ZlibError("download.too_large", "boom")
        work_dir = self.tool.new_work_dir(3600)[1]
        self.tool.run_download(7, work_dir, token)
        snap = ZlibraryTool.snapshot()
        self.assertEqual(snap["status"], "failed")
        self.assertEqual(snap["error"], "download.too_large")
        self.assertIn("danger", [m[0] for m in self.api.msgs])
        self.assertFalse(os.path.exists(work_dir))


class AccountSmoke(SmokeBase):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
