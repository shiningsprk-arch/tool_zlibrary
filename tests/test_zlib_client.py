# -*- coding: utf-8 -*-
"""客户端离线单测：挑战 solver、HTML/EAPI 解析、DoH 分支、参数构造。"""
import hashlib
import os
import sys
import time
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import zlib_client  # noqa: E402


class FakeCookies(object):
    def __init__(self):
        self.values = {}

    def set(self, name, value, domain=None, path=None):
        self.values[name] = value


class FakeSession(object):
    def __init__(self):
        self.cookies = FakeCookies()


class FakeResponse(object):
    def __init__(self, text, url):
        self.text = text
        self.url = url


class ChallengeTest(unittest.TestCase):
    def test_solves_observed_sha1_pow(self):
        token = "3" + "A" * 39
        index = int(token[0], 16)
        expected = None
        for i in range(2_000_000):
            digest = hashlib.sha1((token + str(i)).encode("ascii")).digest()
            if digest[index] == 0xB0 and digest[index + 1] == 0x0B:
                expected = i
                break
        self.assertIsNotNone(expected)
        text = ("<html><script>a0_0x2a54=['%s','c_token=','array'];"
                "let c=a0_0x4457('0x2');let n1=parseInt('0x'+c[0x0]);"
                "</script></html>" % token)
        session = FakeSession()
        fake = FakeResponse(text, "https://zh.example.test/")
        solved = zlib_client._solve_challenge(session, fake)
        self.assertEqual(solved, expected)
        self.assertEqual(session.cookies.values["c_token"], token + str(expected))
        self.assertIn("c_time", session.cookies.values)

    def test_missing_token_raises(self):
        with self.assertRaises(zlib_client.ZlibError) as ctx:
            zlib_client._solve_challenge(FakeSession(), FakeResponse("<html></html>", "https://x/"))
        self.assertEqual(ctx.exception.code, "challenge.unsupported")


class ParseTest(unittest.TestCase):
    def test_parse_bookcards(self):
        html = """
        <z-bookcard id="123" href="/book/123/x.html" download="/dl/token"
          extension="epub" filesize="1.2 MB" language="English" year="2020"
          publisher="P"><div slot="title">Title</div>
          <div slot="author">Author</div></z-bookcard>
        """
        books = zlib_client._parse_bookcards(html, "https://origin.test")
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["id"], "123")
        self.assertEqual(books[0]["title"], "Title")
        self.assertEqual(books[0]["author"], "Author")
        self.assertEqual(books[0]["download"], "/dl/token")
        self.assertEqual(books[0]["extension"], "epub")

    def test_normalize_eapi_book(self):
        book = zlib_client._normalize_eapi_book({
            "id": 1, "hash": "abc", "title": "T", "author": "A",
            "extension": "EPUB", "filesizeString": "1 MB", "dl": "/dl/x",
        }, "https://origin.test")
        self.assertEqual(book["id"], "1")
        self.assertEqual(book["extension"], "epub")
        self.assertEqual(book["download"], "/dl/x")

    def test_sanitize_filename(self):
        self.assertEqual(zlib_client.sanitize_filename('a/b:c*..', "x"),
                         "a b c")
        self.assertEqual(zlib_client.sanitize_filename("", "fallback"), "fallback")

    def test_http_entry_is_upgraded_and_http_download_rejected(self):
        self.assertEqual(zlib_client._normalize_entry("http://zlib.ch"),
                         "https://zlib.ch/")
        with self.assertRaises(zlib_client.ZlibError):
            zlib_client._validate_url("http://127.0.0.1/x")


class DnsTest(unittest.TestCase):
    def test_parse_doh_answers_filters_private_and_dupes(self):
        payload = {"Answer": [
            {"type": 1, "data": "104.21.30.30"},
            {"type": 1, "data": "104.21.30.30"},
            {"type": 1, "data": "127.0.0.1"},
            {"type": 28, "data": "2606:4700::1"},
        ]}
        self.assertEqual(zlib_client._parse_doh_answers(payload),
                         ["104.21.30.30"])

    def test_doh_race_uses_fast_provider_and_caches(self):
        resolver = zlib_client.DnsResolver(mode="doh")
        providers = (("slow", "https://slow.test", 0.5),
                     ("fast", "https://fast.test", 0.5))
        calls = []

        def fake_worker(provider, host, results):
            calls.append(provider[0])
            if provider[0] == "fast":
                time.sleep(0.05)
                resolver._record_report(provider[0], True, 0.05)
                results.put((provider[0], ["104.21.30.30"], "", 0.05))
            else:
                time.sleep(0.35)
                resolver._record_report(provider[0], False, 0.35, "timed out")
                results.put((provider[0], [], "timed out", 0.35))

        with mock.patch.object(resolver, "DOH_PROVIDERS", providers), \
                mock.patch.object(resolver, "DOH_OPTIONAL_PROVIDERS", ()), \
                mock.patch.object(resolver, "_doh_worker", fake_worker):
            started = time.time()
            self.assertEqual(resolver._resolve_doh("race.test"), "104.21.30.30")
            self.assertLess(time.time() - started, 0.5)
            self.assertEqual(resolver._cache_get("race.test"), "104.21.30.30")
            first_calls = len(calls)
            self.assertEqual(resolver._resolve_doh("race.test"), "104.21.30.30")
            self.assertEqual(len(calls), first_calls)
        report = resolver.report()
        self.assertTrue(any(item["provider"] == "fast" and item["ok"]
                            for item in report))

    def test_doh_all_fail_reports_provider_errors(self):
        resolver = zlib_client.DnsResolver(mode="doh")
        providers = (("one", "https://one.test", 0.2),
                     ("two", "https://two.test", 0.2))

        def fake_worker(provider, host, results):
            results.put((provider[0], [], "timeout", 0.1))

        with mock.patch.object(resolver, "DOH_PROVIDERS", providers), \
                mock.patch.object(resolver, "DOH_OPTIONAL_PROVIDERS", ()), \
                mock.patch.object(resolver, "_doh_worker", fake_worker):
            with self.assertRaises(zlib_client.ZlibError) as ctx:
                resolver._resolve_doh("fail.test")
        self.assertEqual(ctx.exception.code, "dns.unreachable")
        self.assertIn("one: timeout", str(ctx.exception))
        self.assertIn("two: timeout", str(ctx.exception))

    def test_doh_fallback_when_system_returns_unspecified(self):
        resolver = zlib_client.DnsResolver(mode="auto")
        with mock.patch("socket.getaddrinfo", return_value=[
                (2, 1, 6, "", ("0.0.0.0", 443))]):
            with mock.patch.object(resolver, "_resolve_doh", return_value="104.21.30.30") as doh:
                self.assertEqual(resolver.resolve("zlib.ch"), "104.21.30.30")
                doh.assert_called_once_with("zlib.ch")

    def test_public_system_ip_uses_default(self):
        resolver = zlib_client.DnsResolver(mode="auto")
        with mock.patch("socket.getaddrinfo", return_value=[
                (2, 1, 6, "", ("104.21.30.30", 443))]):
            self.assertIsNone(resolver.resolve("example.test"))

    def test_private_system_ip_is_rejected(self):
        resolver = zlib_client.DnsResolver(mode="auto")
        with mock.patch("socket.getaddrinfo", return_value=[
                (2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(zlib_client.ZlibError) as ctx:
                resolver.resolve("localhost.test")
        self.assertEqual(ctx.exception.code, "dns.private")


class OriginRetryTest(unittest.TestCase):
    def make_client(self):
        return zlib_client.ZlibClient("https://zlib.ch/", "auto", 5)

    def test_eapi_retries_once_with_new_origin(self):
        client = self.make_client()
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                raise zlib_client.ZlibError("entry.unreachable", "Read timed out")
            response = mock.Mock(status_code=200)
            response.json.return_value = {"success": 1}
            return response

        with mock.patch.object(client, "_request", fake_request), \
                mock.patch.object(client, "origin",
                                  return_value="https://new.example") as origin, \
                mock.patch.object(client.doorway, "invalidate") as invalidate:
            data = client._eapi_json("GET", "https://old.example",
                                     "/eapi/info/domains")
        self.assertEqual(data["success"], 1)
        invalidate.assert_called_once_with("https://old.example")
        origin.assert_called_once_with()
        self.assertTrue(calls[1].startswith("https://new.example"))

    def test_eapi_retries_only_once(self):
        client = self.make_client()
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append(url)
            raise zlib_client.ZlibError("entry.unreachable", "Read timed out")

        with mock.patch.object(client, "_request", fake_request), \
                mock.patch.object(client, "origin",
                                  return_value="https://new.example"), \
                mock.patch.object(client.doorway, "invalidate") as invalidate:
            with self.assertRaises(zlib_client.ZlibError):
                client._eapi_json("GET", "https://old.example", "/eapi/info/domains")
        self.assertEqual(len(calls), 2)
        self.assertEqual(invalidate.call_count, 1)

    def test_download_prepare_retries_after_http_error(self):
        client = self.make_client()
        calls = []

        def fake_once(origin, book, account=None, timeout=None):
            calls.append(origin)
            if len(calls) == 1:
                raise zlib_client.ZlibError("download.http", "HTTP 503")
            return {"url": "https://cdn.example/book.epub"}

        with mock.patch.object(client, "_prepare_download_once", fake_once), \
                mock.patch.object(client, "origin",
                                  return_value="https://new.example"), \
                mock.patch.object(client.doorway, "invalidate") as invalidate:
            target = client.prepare_download(
                "https://old.example", {"id": "1"}, account=None)
        self.assertEqual(target["url"], "https://cdn.example/book.epub")
        self.assertEqual(calls, ["https://old.example", "https://new.example"])
        invalidate.assert_called_once_with("https://old.example")

    def test_final_origin_invalidate_forces_reresolve(self):
        doorway = zlib_client.DoorwaySession("https://zlib.ch/", 5, "auto")
        doorway.final_origin = "https://old.example"
        doorway.final_at = time.time()
        doorway.hops = [{"url": "https://old.example", "status": 200}]
        doorway.invalidate()
        self.assertEqual(doorway.final_origin, "")
        self.assertEqual(doorway.hops, [])
        with mock.patch.object(doorway, "_resolve",
                               return_value="https://new.example") as resolve:
            self.assertEqual(doorway.final(), "https://new.example")
        resolve.assert_called_once_with()

    def test_invalidate_does_not_clear_newer_origin(self):
        doorway = zlib_client.DoorwaySession("https://zlib.ch/", 5, "auto")
        doorway.final_origin = "https://new.example"
        doorway.final_at = time.time()
        self.assertFalse(doorway.invalidate("https://old.example"))
        self.assertEqual(doorway.final_origin, "https://new.example")

    def test_private_literal_is_rejected(self):
        resolver = zlib_client.DnsResolver(mode="auto")
        with self.assertRaises(zlib_client.ZlibError) as ctx:
            resolver.resolve("10.0.0.1")
        self.assertEqual(ctx.exception.code, "dns.private")


class DownloadLimitTest(unittest.TestCase):
    def test_guest_html_limit_page_maps_to_download_limit(self):
        class Headers(dict):
            pass

        class Response(object):
            def __init__(self):
                self.status_code = 200
                self.headers = Headers({"Content-Type": "text/html; charset=utf-8"})
                self.text = ('<section class="download-limits-error">'
                             '<h1>Daily limit reached</h1>'
                             '<p>There are more than 5 downloads from your IP.</p>'
                             '<a>Log In</a></section>')
                self.url = "https://origin.test/dl/x"

            def close(self):
                pass

        client = zlib_client.ZlibClient("https://example.test/", "system", 5)
        client.doorway.session.get = lambda *a, **k: Response()
        with self.assertRaises(zlib_client.ZlibError) as ctx:
            client._prepare_html_download(
                "https://origin.test", "/dl/x", account=None, timeout=5)
        self.assertEqual(ctx.exception.code, "download.limit")


class EapiTest(unittest.TestCase):
    def test_search_uses_form_fields(self):
        client = zlib_client.ZlibClient("https://example.test/", "system", 5)
        captured = {}

        def fake_json(method, origin, path, **kwargs):
            captured.update({"method": method, "origin": origin, "path": path,
                             "kwargs": kwargs})
            return {"books": [], "pagination": {}}

        client._eapi_json = fake_json
        client.eapi_search("https://origin.test", "三体", 2, 20, {
            "languages": ["zh"], "extensions": ["EPUB"],
            "year_from": 2000, "year_to": 2024, "order": "newest", "exact": True,
        })
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/eapi/book/search")
        data = captured["kwargs"]["data"]
        self.assertEqual(data["message"], "三体")
        self.assertEqual(data["page"], "2")
        self.assertEqual(data["limit"], "20")
        self.assertEqual(data["languages[]"], ["zh"])
        self.assertEqual(data["extensions[]"], ["epub"])
        self.assertEqual(data["yearFrom"], "2000")
        self.assertEqual(data["yearTo"], "2024")
        self.assertEqual(data["order"], "newest")
        self.assertEqual(data["e"], "1")

    def test_profile_quota_mapping(self):
        client = zlib_client.ZlibClient("https://example.test/", "system", 5)
        client._eapi_json = lambda *a, **k: {
            "user": {"downloads_today": 3, "downloads_limit": 10,
                     "email": "u@example.test", "isPremium": False}}
        profile = client.eapi_profile("https://origin.test", {"cookies": {}})
        self.assertEqual(profile["quota_today"], 3)
        self.assertEqual(profile["quota_limit"], 10)
        self.assertEqual(profile["remaining"], 7)

    def test_account_cookies_drop_challenge(self):
        client = zlib_client.ZlibClient("https://example.test/", "system", 5)
        cookies = client._account_cookies({
            "cookies": {"remix_userid": "1", "remix_userkey": "k",
                        "c_token": "x", "c_time": "y"},
            "remix_userid": "1", "remix_userkey": "k",
        })
        self.assertEqual(cookies["remix_userid"], "1")
        self.assertEqual(cookies["remix_userkey"], "k")
        self.assertNotIn("c_token", cookies)
        self.assertNotIn("c_time", cookies)

    def test_eapi_error_classification(self):
        self.assertEqual(zlib_client._eapi_error("Please login").code,
                         "accounts.session_expired")
        self.assertEqual(zlib_client._eapi_error("daily limit reached").code,
                         "download.limit")
        self.assertEqual(zlib_client._eapi_error("Incorrect email or password").code,
                         "accounts.login_failed")
        self.assertEqual(zlib_client._eapi_error("Authorization failed").code,
                         "accounts.login_failed")
        self.assertEqual(zlib_client._eapi_error("weird").code, "eapi.rejected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
