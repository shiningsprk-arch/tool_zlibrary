# -*- coding: utf-8 -*-
"""工具包契约守卫（纯标准库：AST + JSON + 正则，不 import 宿主 webserver）。"""
import ast
import json
import os
import re
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
LOCALE_CODES = ("zh", "en", "zh-TW")
DEFAULT_LOCALE = "zh"

REQUIRED_FIELDS = (
    "tool_id", "name", "description", "revision", "author",
    "core_api_version", "entry_backend", "repo_url",
)
TOOL_ID_RE = re.compile(r"^[a-z0-9_]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
INFO_FIELDS = ("tool_id", "name", "description", "revision", "author",
               "publish_date", "repo_url")

API_ROUTE_PATHS = ["accounts", "cancel", "config", "download", "health",
                   "progress", "search"]
HANDLER_METHODS = {
    "ConfigHandler": ("get", "post"),
    "HealthHandler": ("get",),
    "AccountsHandler": ("get", "post"),
    "SearchHandler": ("post",),
    "DownloadHandler": ("post",),
    "ProgressHandler": ("get",),
    "CancelHandler": ("post",),
}
EXTRA_ERROR_CODES = {"request_failed", "unknown"}
DYNAMIC_I18N_KEYS = {
    "stagePrepare", "stageDownloading", "stageImporting", "stageDone",
    "accountStatusOk", "accountStatusExhausted", "accountStatusInvalid",
    "accountStatusError", "accountStatusUnknown",
}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_json(path):
    return json.loads(_read(path))


def _load_locales():
    return {code: _read_json(os.path.join(ROOT, "frontend", "locales", "%s.json" % code))
            for code in LOCALE_CODES}


def _class_node(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError("backend/tool.py 里找不到类 %s" % name)


def _method_node(class_node, name):
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError("类 %s 里找不到方法 %s" % (class_node.name, name))


def _decorator_names(method_node):
    names = []
    for dec in method_node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Attribute):
            names.append(target.attr)
    return names


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.manifest = _read_json(os.path.join(ROOT, "manifest.json"))

    def test_required_fields_present(self):
        missing = [f for f in REQUIRED_FIELDS if not self.manifest.get(f)]
        self.assertEqual(missing, [], "manifest.json 缺少必填字段：%s" % missing)

    def test_tool_id_and_versions(self):
        self.assertRegex(self.manifest["tool_id"], TOOL_ID_RE)
        self.assertRegex(self.manifest["revision"], SEMVER_RE)
        self.assertEqual(self.manifest["revision"], "0.1.1")
        self.assertRegex(self.manifest["core_api_version"], SEMVER_RE)

    def test_repo_url_is_absolute_https(self):
        self.assertTrue(self.manifest["repo_url"].startswith("https://"))

    def test_locales_and_default(self):
        self.assertIn(self.manifest["default_locale"], self.manifest["locales"])

    def test_entry_points(self):
        self.assertEqual(self.manifest["entry_frontend"], "index.html")
        module_rel, _, class_name = self.manifest["entry_backend"].rpartition(".")
        self.assertEqual(module_rel, "tool")
        self.assertEqual(class_name, "ZlibraryTool")

    def test_api_routes_declared(self):
        routes = self.manifest["api_routes"]
        paths = [entry["path"] for entry in routes]
        self.assertEqual(sorted(paths), API_ROUTE_PATHS)
        for entry in routes:
            self.assertTrue(entry["handler"].startswith("tool."))
            self.assertFalse(entry["path"].startswith("/"))

    def test_no_frontend_build_step(self):
        config = _read_json(os.path.join(ROOT, ".toolbuilder.json"))
        self.assertIsNone(config["frontendBuildCommand"])
        self.assertIsNone(config["frontendOutputDir"])


class PackageLayoutTest(unittest.TestCase):
    def test_backend_package_and_entry_module(self):
        for rel in ("backend/__init__.py", "backend/tool.py",
                    "backend/zlib_client.py", "backend/accounts.py"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)

    def test_frontend_entry_exists(self):
        html = _read(os.path.join(ROOT, "frontend", "index.html"))
        self.assertIn('src="/static/toolbox-bridge.js"', html)
        self.assertIn('src="lib/i18n.js', html)
        self.assertIn('src="app.js', html)

    def test_icon_is_png_at_package_root(self):
        icon = os.path.join(ROOT, "icon.png")
        self.assertTrue(os.path.isfile(icon), "动态工具的图标只认包根 icon.png")
        with open(icon, "rb") as f:
            head = f.read(24)
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
        width = int.from_bytes(head[16:20], "big")
        height = int.from_bytes(head[20:24], "big")
        self.assertEqual(width, height, "icon.png 必须是正方形（%dx%d）" % (width, height))
        self.assertGreaterEqual(width, 256)

    def test_no_bundled_dependency_copies(self):
        for dirpath, dirnames, _files in os.walk(os.path.join(ROOT, "backend")):
            for name in dirnames:
                self.assertNotIn(name, ("vendor", "site-packages"))

    def test_required_runtime_files_exist(self):
        for rel in ("backend/tool.py", "backend/zlib_client.py", "backend/accounts.py",
                    "LICENSE", "README.md"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), "缺少 %s" % rel)

    def test_mobile_layout_and_row_progress(self):
        html = _read(os.path.join(ROOT, "frontend", "index.html"))
        js = _read(os.path.join(ROOT, "frontend", "app.js"))
        self.assertIn("@media (max-width: 640px)", html)
        self.assertIn(".row-progress", html)
        self.assertIn("overflow-wrap: anywhere", html)
        self.assertIn("function mountProgress", js)
        self.assertIn("holder.appendChild(dom.progressArea)", js)
        # 重新进入工具时只恢复进行中任务，不回放旧 done/error
        self.assertNotIn(
            "status === 'completed' || status === 'failed' || status === 'cancelled'",
            js)

    def test_default_account_mode_and_done_link(self):
        html = _read(os.path.join(ROOT, "frontend", "index.html"))
        js = _read(os.path.join(ROOT, "frontend", "app.js"))
        self.assertRegex(
            html,
            re.compile(
                r'<select id="account-mode"[^>]*>.*?<option value="password"[^>]*selected',
                re.S),
            "添加账号默认应选邮箱+密码")
        self.assertRegex(html, r'<div id="password-fields"(?![^>]*hidden)')
        self.assertRegex(html, r'<div id="cookie-fields"[^>]*hidden')
        self.assertNotIn('data-i18n="openBook"', html)
        self.assertIn("dom.openBook.textContent = title || i18n.t('openBook')", js)


class BackendContractTest(unittest.TestCase):
    def setUp(self):
        self.tool_src = _read(os.path.join(ROOT, "backend", "tool.py"))
        self.tree = ast.parse(self.tool_src)
        self.manifest = _read_json(os.path.join(ROOT, "manifest.json"))

    def test_info_matches_manifest(self):
        info_fn = [n for n in ast.walk(self.tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "info"]
        self.assertTrue(info_fn)
        returns = [s for s in info_fn[0].body if isinstance(s, ast.Return)]
        self.assertEqual(len(returns), 1)
        info = ast.literal_eval(returns[0].value)
        for field in INFO_FIELDS:
            self.assertEqual(info.get(field), self.manifest.get(field),
                             "info().%s 与 manifest.json 不一致" % field)

    def test_tool_class_subclasses_base_tool(self):
        cls = _class_node(self.tree, self.manifest["entry_backend"].split(".")[-1])
        bases = [b.id if isinstance(b, ast.Name) else getattr(b, "attr", None)
                 for b in cls.bases]
        self.assertIn("BaseTool", bases)

    def test_every_handler_method_requires_admin_and_js(self):
        for cls_name, methods in HANDLER_METHODS.items():
            cls = _class_node(self.tree, cls_name)
            for method_name in methods:
                decorators = _decorator_names(_method_node(cls, method_name))
                self.assertIn("js", decorators, "%s.%s" % (cls_name, method_name))
                self.assertIn("is_admin", decorators, "%s.%s" % (cls_name, method_name))
                self.assertLess(decorators.index("js"), decorators.index("is_admin"),
                                "%s.%s 的 @js 必须在 @is_admin 外层" % (cls_name, method_name))

    def test_download_never_takes_client_urls(self):
        method = _method_node(_class_node(self.tree, "DownloadHandler"), "post")
        segment = ast.get_source_segment(self.tool_src, method) or ""
        self.assertIn("load_book", segment)
        self.assertNotIn('body.get("url")', segment)

    def test_remote_handlers_run_via_executor(self):
        for cls_name, fn in (("SearchHandler", "search_sync"),
                             ("AccountsHandler", "account_action"),
                             ("HealthHandler", "health_sync")):
            segment = ast.get_source_segment(
                self.tool_src, _method_node(_class_node(self.tree, cls_name), "post"
                                            if cls_name != "HealthHandler" else "get")) or ""
            self.assertIn("run_in_executor", segment, cls_name)
            self.assertIn(fn, segment, cls_name)

    def test_download_handler_starts_async_task(self):
        segment = ast.get_source_segment(
            self.tool_src, _method_node(_class_node(self.tree, "DownloadHandler"), "post")) or ""
        self.assertIn("run_download", segment)
        self.assertIn("_begin", segment)

    def test_no_direct_db_access(self):
        self.assertNotIn("self.db", self.tool_src)
        self.assertNotIn("self.session", self.tool_src)

    def test_routes_are_manifest_driven(self):
        self.assertNotIn("/api/toolbox/zlibrary/", self.tool_src)
        self.assertIn("/api/toolbox/tool/zlibrary", self.tool_src)


class I18nTest(unittest.TestCase):
    def setUp(self):
        self.locales = _load_locales()
        self.html = _read(os.path.join(ROOT, "frontend", "index.html"))
        self.js = _read(os.path.join(ROOT, "frontend", "app.js"))

    def test_locale_files_match_locales_manifest(self):
        declared = _read_json(os.path.join(ROOT, "frontend", "locales", "manifest.json"))
        on_disk = sorted(name[:-5] for name in os.listdir(
            os.path.join(ROOT, "frontend", "locales")) if name.endswith(".json")
            and name != "manifest.json")
        self.assertEqual(sorted(declared["locales"]), on_disk)
        self.assertIn(declared["default"], declared["locales"])
        tool_manifest = _read_json(os.path.join(ROOT, "manifest.json"))
        self.assertEqual(sorted(tool_manifest["locales"]), on_disk)

    def test_catalogs_are_flat_string_tables(self):
        for code, table in self.locales.items():
            for key, value in table.items():
                self.assertIsInstance(value, str, "%s.json: %s 不是字符串" % (code, key))

    def test_key_sets_identical(self):
        reference = set(self.locales[DEFAULT_LOCALE])
        for code, table in self.locales.items():
            self.assertEqual(set(table), reference,
                             "%s.json 的 key 集合与 %s.json 不一致" % (code, DEFAULT_LOCALE))

    def test_html_keys_are_translated(self):
        keys = set(re.findall(r'data-i18n="([^"]+)"', self.html))
        for spec in re.findall(r'data-i18n-attr="([^"]+)"', self.html):
            for rule in spec.split(";"):
                parts = rule.split(":")
                if len(parts) == 2:
                    keys.add(parts[1].strip())
        self.assertTrue(keys)
        for code, table in self.locales.items():
            missing = sorted(k for k in keys if k not in table)
            self.assertEqual(missing, [], "%s.json 缺少 index.html 引用的文案：%s" % (code, missing))

    def test_js_literal_keys_are_translated(self):
        keys = set(re.findall(r"i18n\.t\('([^']+)'", self.js))
        keys |= set(re.findall(r'i18n\.t\("([^"]+)"', self.js))
        keys = {k for k in keys if not k.startswith(("error.", "accountStatus"))}
        keys |= DYNAMIC_I18N_KEYS
        self.assertTrue(keys, "app.js 里没有找到 i18n.t 调用？")
        for code, table in self.locales.items():
            missing = sorted(k for k in keys if k not in table)
            self.assertEqual(missing, [], "%s.json 缺少 app.js 引用的文案：%s" % (code, missing))

    def test_backend_codes_are_translated(self):
        codes = set(EXTRA_ERROR_CODES)
        for rel in ("backend/zlib_client.py", "backend/tool.py"):
            src = _read(os.path.join(ROOT, rel))
            codes |= set(re.findall(r'ZlibError\(\s*"([a-z0-9_.]+)"', src))
            codes |= set(re.findall(r'"err":\s*"([a-z0-9_.]+)"', src)) - {"ok"}
        self.assertTrue(codes)
        for code, table in self.locales.items():
            missing = sorted(c for c in codes if "error.%s" % c not in table)
            self.assertEqual(missing, [], "%s.json 缺少错误码文案：%s" % (code, missing))


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=2))
