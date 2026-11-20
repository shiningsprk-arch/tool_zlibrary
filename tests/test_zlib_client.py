# -*- coding: utf-8 -*-
"""客户端离线单测：挑战 solver、HTML/EAPI 解析、DoH 分支、参数构造。"""
import hashlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import requests

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import zlib_client  # noqa: E402


class FakeCookies(object):
    def __init__(self):
        self.values = {}

    def set(self, name, value, domain=None, path=None):
        self.values[name] = value

    def update(self, other):
        if isinstance(other, FakeCookies):
            self.values.update(other.values)
        else:
            for cookie in other:
                self.values[cookie.name] = cookie.value


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
        self.assertEqual(zlib_client.sanitize_filename("", "a/b"), "a b")
        long_cjk = "中" * 1000
        clipped = zlib_client.sanitize_filename(long_cjk, "fallback")
        self.assertLessEqual(len(clipped.encode("utf-8")), 200)
        self.assertLessEqual(len((clipped + ".epub").encode("utf-8")), 209)
        emoji = zlib_client.sanitize_filename("😀" * 1000, "fallback")
        self.assertLessEqual(len(emoji.encode("utf-8")), 200)
        self.assertEqual(zlib_client.sanitize_filename("", "x" * 1000),
                         "x" * 200)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, clipped + ".epub")
            with open(dest, "wb") as handle:
                handle.write(b"PK")
            self.assertTrue(os.path.exists(dest))

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


class EntryRetryTest(unittest.TestCase):
    class FakeSession(object):
        def __init__(self, error=None, response=None):
            self.error = error
            self.response = response
            self.calls = 0
            self.closed = False
            self.cookies = FakeCookies()

        def get(self, url, **kwargs):
            self.calls += 1
            if self.error is not None:
                raise self.error
            return self.response

        def close(self):
            self.closed = True

    def make_doorway(self):
        doorway = zlib_client.DoorwaySession("https://zlib.ch/", 30, "auto")
        return doorway

    def test_entry_timeout_retries_with_new_session(self):
        doorway = self.make_doorway()
        first = self.FakeSession(error=requests.exceptions.ReadTimeout("stalled"))
        second = self.FakeSession(response=mock.Mock(
            status_code=200, url="https://zlib.ch/"))
        doorway.session = first
        with mock.patch.object(doorway, "_new_session", return_value=second), \
                mock.patch.object(zlib_client, "_ENTRY_RETRY_DELAY", 0):
            response = doorway._request("https://zlib.ch/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(first.calls, 1)
        self.assertTrue(first.closed)
        self.assertEqual(second.calls, 1)
        self.assertIs(doorway.session, second)

    def test_entry_retry_preserves_challenge_cookies(self):
        doorway = self.make_doorway()
        first = self.FakeSession(error=requests.exceptions.ReadTimeout("stalled"))
        first.cookies.set("c_token", "solved")
        second = self.FakeSession(response=mock.Mock(
            status_code=200, url="https://zlib.ch/"))
        doorway.session = first
        with mock.patch.object(doorway, "_new_session", return_value=second), \
                mock.patch.object(zlib_client, "_ENTRY_RETRY_DELAY", 0):
            doorway._request("https://zlib.ch/")
        self.assertEqual(second.cookies.values.get("c_token"), "solved")

    def test_entry_retry_stops_after_limit(self):
        doorway = self.make_doorway()
        first = self.FakeSession(error=requests.exceptions.ReadTimeout("one"))
        second = self.FakeSession(error=requests.exceptions.ReadTimeout("two"))
        doorway.session = first
        with mock.patch.object(doorway, "_new_session", return_value=second), \
                mock.patch.object(zlib_client, "_ENTRY_RETRY_DELAY", 0):
            with self.assertRaises(zlib_client.ZlibError) as ctx:
                doorway._request("https://zlib.ch/")
        self.assertEqual(ctx.exception.code, "entry.unreachable")
        self.assertIn("zlib.ch", str(ctx.exception))
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)

    def test_entry_cert_error_is_not_retried(self):
        doorway = self.make_doorway()
        first = self.FakeSession(error=requests.exceptions.SSLError(
            "certificate verify failed"))
        second = self.FakeSession()
        doorway.session = first
        with mock.patch.object(doorway, "_new_session", return_value=second):
            with self.assertRaises(zlib_client.ZlibError) as ctx:
                doorway._request("https://zlib.ch/")
        self.assertEqual(ctx.exception.code, "entry.unreachable")
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 0)

    class FakeHttpResponse(object):
        def __init__(self, status_code, url, location="", text=""):
            self.status_code = status_code
            self.url = url
            self.headers = {}
            if location:
                self.headers["Location"] = location
            self.text = text
            self.closed = False

        def close(self):
            self.closed = True

    class QueueSession(object):
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = 0
            self.closed = False
            self.cookies = FakeCookies()

        def get(self, url, **kwargs):
            self.calls += 1
            return self.responses.pop(0)

        def close(self):
            self.closed = True

    def test_entry_http_504_retries_with_new_session(self):
        doorway = self.make_doorway()
        first = self.FakeHttpResponse(504, "https://zlib.ch/")
        ok = self.FakeHttpResponse(200, "https://zlib.ch/")
        session = self.QueueSession([first, ok])
        doorway.session = session
        with mock.patch.object(doorway, "_new_session", return_value=session), \
                mock.patch.object(zlib_client, "_ENTRY_HTTP_RETRY_DELAY", 0):
            response = doorway._request("https://zlib.ch/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.calls, 2)
        self.assertTrue(first.closed)

    def test_entry_http_challenge_503_is_not_retried(self):
        doorway = self.make_doorway()
        challenge = self.FakeHttpResponse(
            503, "https://zlib.ch/",
            text="<html>Checking your browser a0_0x2a54</html>")
        session = self.QueueSession([challenge])
        doorway.session = session
        with mock.patch.object(doorway, "_new_session", return_value=session):
            response = doorway._request("https://zlib.ch/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(session.calls, 1)

    def test_entry_http_5xx_stops_after_retry_limit(self):
        doorway = self.make_doorway()
        responses = [
            self.FakeHttpResponse(504, "https://zlib.ch/") for _ in range(3)]
        session = self.QueueSession(responses)
        doorway.session = session
        with mock.patch.object(doorway, "_new_session", return_value=session), \
                mock.patch.object(zlib_client, "_ENTRY_HTTP_RETRY_DELAY", 0):
            response = doorway._request("https://zlib.ch/")
        self.assertEqual(response.status_code, 504)
        self.assertEqual(session.calls, 3)

    def test_resolve_reports_entry_http_after_5xx_retries(self):
        doorway = self.make_doorway()
        redirect = self.FakeHttpResponse(
            302, "https://zlib.ch/", location="https://mirror.example/")
        responses = [redirect] + [
            self.FakeHttpResponse(504, "https://mirror.example/")
            for _ in range(3)]
        session = self.QueueSession(responses)
        doorway.session = session
        with mock.patch.object(doorway, "_new_session", return_value=session), \
                mock.patch.object(zlib_client, "_ENTRY_HTTP_RETRY_DELAY", 0):
            with self.assertRaises(zlib_client.ZlibError) as ctx:
                doorway._resolve()
        self.assertEqual(ctx.exception.code, "entry.http")
        self.assertIn("HTTP 504", ctx.exception.message)
        self.assertIn("mirror.example", ctx.exception.message)
        self.assertEqual(session.calls, 4)


class TolerantOriginTest(unittest.TestCase):
    def make_doorway(self):
        return zlib_client.DoorwaySession("https://zlib.ch/", 5, "auto")

    def test_tolerant_origin_accepts_5xx_landing_without_challenge(self):
        doorway = self.make_doorway()
        redirect = EntryRetryTest.FakeHttpResponse(
            302, "https://zlib.ch/", location="https://mirror.example/")
        landing = EntryRetryTest.FakeHttpResponse(
            504, "https://mirror.example/")
        session = EntryRetryTest.QueueSession([redirect, landing])
        doorway.session = session
        with mock.patch.object(doorway, "_new_session", return_value=session):
            origin = doorway.tolerant_origin()
        self.assertEqual(origin, "https://mirror.example")
        self.assertEqual(doorway.tolerant_final, "https://mirror.example")
        self.assertEqual(doorway.final_origin, "")
        self.assertEqual([h["status"] for h in doorway.tolerant_hops],
                         [302, 504])
        self.assertEqual(session.calls, 2)

    def test_tolerant_origin_is_cached_and_cleared_by_invalidate(self):
        doorway = self.make_doorway()
        doorway.tolerant_final = "https://mirror.example"
        doorway.tolerant_at = time.time()
        doorway.tolerant_hops = [{"url": "https://mirror.example", "status": 504}]
        self.assertEqual(doorway.tolerant_origin(), "https://mirror.example")
        doorway.invalidate()
        self.assertEqual(doorway.tolerant_final, "")
        self.assertEqual(doorway.tolerant_hops, [])

    def test_resolve_failure_clears_stale_final_origin(self):
        doorway = self.make_doorway()
        doorway.final_origin = "https://old.example"
        doorway.final_at = time.time()
        doorway.hops = [{"url": "https://old.example", "status": 200}]
        session = EntryRetryTest.QueueSession([
            EntryRetryTest.FakeHttpResponse(504, "https://zlib.ch/")
            for _ in range(3)])
        doorway.session = session
        with mock.patch.object(doorway, "_new_session", return_value=session), \
                mock.patch.object(zlib_client, "_ENTRY_HTTP_RETRY_DELAY", 0):
            with self.assertRaises(zlib_client.ZlibError) as ctx:
                doorway._resolve()
        self.assertEqual(ctx.exception.code, "entry.http")
        self.assertEqual(doorway.final_origin, "")
        self.assertEqual(doorway.hops, [])


class OriginFallbackTest(unittest.TestCase):
    def make_client(self):
        return zlib_client.ZlibClient("https://zlib.ch/", "auto", 5)

    def test_entry_failure_uses_tolerant_origin_when_eapi_probe_passes(self):
        client = self.make_client()
        error = zlib_client.ZlibError(
            "entry.http", "HTTP 504 at https://zh.friday101.ru/")
        with mock.patch.object(client.doorway, "final", side_effect=error), \
                mock.patch.object(client.doorway, "tolerant_origin",
                                  return_value="https://mirror.example"), \
                mock.patch.object(client, "_probe_origin",
                                  return_value=True) as probe:
            origin = client.origin()
        self.assertEqual(origin, "https://mirror.example")
        self.assertEqual(client._api_origin, "https://mirror.example")
        probe.assert_called_once_with("https://mirror.example")

    def test_entry_failure_keeps_original_error_when_probe_fails(self):
        client = self.make_client()
        error = zlib_client.ZlibError(
            "entry.http", "HTTP 504 at https://zh.friday101.ru/")
        with mock.patch.object(client.doorway, "final", side_effect=error), \
                mock.patch.object(client.doorway, "tolerant_origin",
                                  return_value="https://mirror.example"), \
                mock.patch.object(client, "_probe_origin", return_value=False):
            with self.assertRaises(zlib_client.ZlibError) as ctx:
                client.origin()
        self.assertEqual(ctx.exception.code, "entry.http")
        self.assertIn("HTTP 504", ctx.exception.message)

    def test_entry_failure_without_tolerant_origin_raises_original(self):
        client = self.make_client()
        error = zlib_client.ZlibError("entry.unreachable", "timed out")
        with mock.patch.object(client.doorway, "final", side_effect=error), \
                mock.patch.object(client.doorway, "tolerant_origin",
                                  side_effect=zlib_client.ZlibError(
                                      "entry.unreachable", "timed out")):
            with self.assertRaises(zlib_client.ZlibError) as ctx:
                client.origin()
        self.assertEqual(ctx.exception.code, "entry.unreachable")

    def test_strict_origin_is_preferred(self):
        client = self.make_client()
        with mock.patch.object(client.doorway, "final",
                               return_value="https://strict.example"), \
                mock.patch.object(client.doorway, "tolerant_origin") as tolerant, \
                mock.patch.object(client, "_probe_origin") as probe:
            origin = client.origin()
        self.assertEqual(origin, "https://strict.example")
        tolerant.assert_not_called()
        probe.assert_not_called()

    def test_fallback_origin_is_reused_until_invalidated(self):
        client = self.make_client()
        error = zlib_client.ZlibError("entry.http", "HTTP 504")
        with mock.patch.object(client.doorway, "final", side_effect=error), \
                mock.patch.object(client.doorway, "tolerant_origin",
                                  return_value="https://mirror.example") as tolerant, \
                mock.patch.object(client, "_probe_origin", return_value=True):
            self.assertEqual(client.origin(), "https://mirror.example")
            self.assertEqual(client.origin(), "https://mirror.example")
        self.assertEqual(tolerant.call_count, 1)
        client.invalidate_origin("https://mirror.example")
        with mock.patch.object(client.doorway, "final", side_effect=error), \
                mock.patch.object(client.doorway, "tolerant_origin",
                                  return_value="https://mirror.example") as tolerant, \
                mock.patch.object(client, "_probe_origin", return_value=True):
            self.assertEqual(client.origin(), "https://mirror.example")
        self.assertEqual(tolerant.call_count, 1)

    def test_force_origin_bypasses_fallback_cache(self):
        client = self.make_client()
        error = zlib_client.ZlibError("entry.http", "HTTP 504")
        with mock.patch.object(client.doorway, "final", side_effect=error), \
                mock.patch.object(client.doorway, "tolerant_origin",
                                  return_value="https://mirror.example") as tolerant, \
                mock.patch.object(client, "_probe_origin", return_value=True):
            client.origin()
            client.origin(force=True)
        self.assertEqual(tolerant.call_count, 2)

    def test_invalidate_origin_does_not_clear_newer_fallback(self):
        client = self.make_client()
        client._api_origin = "https://new.example"
        client._api_origin_at = time.time()
        with mock.patch.object(client.doorway, "invalidate") as invalidate:
            self.assertFalse(client.invalidate_origin("https://old.example"))
        invalidate.assert_not_called()
        self.assertEqual(client._api_origin, "https://new.example")

    def test_probe_origin_accepts_success_json(self):
        client = self.make_client()
        response = mock.Mock(status_code=200)
        response.json.return_value = {"success": 1}
        with mock.patch.object(client, "_request",
                               return_value=response) as request:
            self.assertTrue(client._probe_origin("https://mirror.example"))
        self.assertTrue(request.call_args[0][1].endswith("/eapi/info/domains"))

    def test_probe_origin_rejects_non_json(self):
        client = self.make_client()
        response = mock.Mock(status_code=504)
        response.json.side_effect = ValueError("not json")
        with mock.patch.object(client, "_request", return_value=response):
            self.assertFalse(client._probe_origin("https://mirror.example"))

    def test_probe_origin_rejects_redirect(self):
        client = self.make_client()
        response = mock.Mock(status_code=302)
        response.json.return_value = {"success": 1}
        with mock.patch.object(client, "_request",
                               return_value=response) as request:
            self.assertFalse(client._probe_origin("https://mirror.example"))
        self.assertFalse(request.call_args.kwargs["allow_redirects"])

    def test_probe_origin_rejects_rejected_json(self):
        client = self.make_client()
        response = mock.Mock(status_code=200)
        response.json.return_value = {"success": 0, "error": "nope"}
        with mock.patch.object(client, "_request", return_value=response):
            self.assertFalse(client._probe_origin("https://mirror.example"))

    def test_force_origin_passes_force_to_tolerant_origin(self):
        client = self.make_client()
        error = zlib_client.ZlibError("entry.http", "HTTP 504")
        with mock.patch.object(client.doorway, "final", side_effect=error), \
                mock.patch.object(client.doorway, "tolerant_origin",
                                  return_value="https://mirror.example") as tolerant, \
                mock.patch.object(client, "_probe_origin", return_value=True):
            client.origin()
            client.origin(force=True)
        self.assertFalse(tolerant.call_args_list[0].kwargs["force"])
        self.assertTrue(tolerant.call_args_list[1].kwargs["force"])

    def test_tolerant_origin_is_dropped_when_eapi_probe_fails(self):
        client = self.make_client()
        client.doorway.tolerant_final = "https://mirror.example"
        client.doorway.tolerant_at = time.time()
        client.doorway.tolerant_hops = [
            {"url": "https://mirror.example", "status": 504}]
        error = zlib_client.ZlibError("entry.http", "HTTP 504")
        with mock.patch.object(client.doorway, "final", side_effect=error), \
                mock.patch.object(client, "_probe_origin", return_value=False):
            with self.assertRaises(zlib_client.ZlibError):
                client.origin()
        self.assertEqual(client.doorway.tolerant_final, "")
        self.assertEqual(client.doorway.tolerant_hops, [])

    def test_eapi_cache_is_scoped_to_origin(self):
        client = self.make_client()
        old = "https://old.example"
        new = "https://new.example"
        with mock.patch.object(
                client, "_eapi_json",
                side_effect=zlib_client.ZlibError("eapi.http", "HTTP 503")), \
                mock.patch.object(client.doorway, "final_origin", old):
            self.assertFalse(client.probe_eapi(old))
        self.assertFalse(client._eapi)
        self.assertEqual(client._eapi_origin, old)
        with mock.patch.object(client, "_eapi_json",
                               return_value={"success": 1}) as request:
            self.assertTrue(client.probe_eapi(new))
        request.assert_called_once()
        self.assertTrue(client._eapi)
        self.assertEqual(client._eapi_origin, new)

    def test_set_api_origin_and_invalidate_clear_eapi_cache(self):
        client = self.make_client()
        client._api_origin = "https://a.example"
        client._api_origin_at = time.time()
        client._eapi = True
        client._eapi_origin = "https://a.example"
        client._set_api_origin("https://b.example")
        self.assertIsNone(client._eapi)
        self.assertEqual(client._eapi_origin, "")
        client._eapi = False
        client._eapi_origin = "https://b.example"
        client.invalidate_origin("https://b.example")
        self.assertIsNone(client._eapi)
        self.assertEqual(client._eapi_origin, "")

    class DualSession(object):
        def __init__(self, get_responses, request_responses):
            self.get_responses = list(get_responses)
            self.request_responses = list(request_responses)
            self.get_calls = 0
            self.request_calls = 0
            self.cookies = FakeCookies()

        def get(self, url, **kwargs):
            self.get_calls += 1
            return self.get_responses.pop(0)

        def request(self, method, url, **kwargs):
            self.request_calls += 1
            return self.request_responses.pop(0)

        def close(self):
            pass

    class JsonResponse(object):
        def __init__(self, status_code, url, payload):
            self.status_code = status_code
            self.url = url
            self.text = ""
            self.headers = {}
            self.payload = payload

        def json(self):
            return self.payload

        def close(self):
            pass

    def test_entry_504_falls_back_to_eapi_verified_origin(self):
        client = self.make_client()
        redirect = lambda: EntryRetryTest.FakeHttpResponse(
            302, "https://zlib.ch/", location="https://mirror.example/")
        landing = lambda: EntryRetryTest.FakeHttpResponse(
            504, "https://mirror.example/")
        probe = self.JsonResponse(
            200, "https://mirror.example/eapi/info/domains", {"success": 1})
        session = self.DualSession(
            get_responses=[
                redirect(), landing(), landing(), landing(),
                redirect(), landing(),
            ],
            request_responses=[probe],
        )
        client.doorway.session = session
        with mock.patch.object(client.doorway, "_new_session",
                               return_value=session), \
                mock.patch.object(zlib_client, "_ENTRY_HTTP_RETRY_DELAY", 0):
            origin = client.origin()
        self.assertEqual(origin, "https://mirror.example")
        self.assertTrue(client._api_is_fallback)
        self.assertEqual(client.doorway.final_origin, "")
        self.assertEqual(client.doorway.tolerant_final,
                         "https://mirror.example")
        self.assertEqual(session.request_calls, 1)
        self.assertEqual(session.get_calls, 6)
        self.assertTrue(client._eapi)
        self.assertEqual(client._eapi_origin, "https://mirror.example")


class HealthTest(unittest.TestCase):
    def test_health_reports_fallback_origin(self):
        client = zlib_client.ZlibClient("https://zlib.ch/", "auto", 5)
        client._api_origin = "https://mirror.example"
        client._api_origin_at = time.time()
        client._api_is_fallback = True
        with mock.patch.object(
                client, "origin", return_value="https://mirror.example"), \
                mock.patch.object(client, "_probe_origin", return_value=True):
            data = client.health()
        self.assertEqual(data["final_origin"], "https://mirror.example")
        self.assertTrue(data["fallback"])
        self.assertTrue(data["eapi"])

    def test_health_does_not_report_fallback_for_strict_origin(self):
        client = zlib_client.ZlibClient("https://zlib.ch/", "auto", 5)
        client._api_origin = "https://strict.example"
        client._api_origin_at = time.time()
        client._api_is_fallback = False
        with mock.patch.object(
                client, "origin", return_value="https://strict.example"), \
                mock.patch.object(client, "_probe_origin", return_value=True):
            data = client.health()
        self.assertFalse(data["fallback"])

    def test_health_reports_fallback_after_strict_failure(self):
        client = zlib_client.ZlibClient("https://zlib.ch/", "auto", 5)
        client.doorway.final_origin = "https://old.example"
        client.doorway.final_at = time.time()
        error = zlib_client.ZlibError("entry.http", "HTTP 504")
        with mock.patch.object(client.doorway, "final", side_effect=error), \
                mock.patch.object(client.doorway, "tolerant_origin",
                                  return_value="https://mirror.example"), \
                mock.patch.object(client, "_probe_origin", return_value=True):
            data = client.health()
        self.assertEqual(data["final_origin"], "https://mirror.example")
        self.assertTrue(data["fallback"])

    def test_health_reuses_cached_eapi_probe_result(self):
        client = zlib_client.ZlibClient("https://zlib.ch/", "auto", 5)
        client._api_origin = "https://mirror.example"
        client._api_origin_at = time.time()
        client._api_is_fallback = True
        client._eapi = True
        client._eapi_origin = "https://mirror.example"
        with mock.patch.object(
                client, "origin", return_value="https://mirror.example"), \
                mock.patch.object(client, "_probe_origin") as probe:
            data = client.health()
        probe.assert_not_called()
        self.assertTrue(data["eapi"])

    def test_health_probes_fresh_for_strict_cached_result(self):
        client = zlib_client.ZlibClient("https://zlib.ch/", "auto", 5)
        client._api_origin = "https://strict.example"
        client._api_origin_at = time.time()
        client._api_is_fallback = False
        client._eapi = True
        client._eapi_origin = "https://strict.example"
        with mock.patch.object(
                client, "origin", return_value="https://strict.example"), \
                mock.patch.object(client, "_probe_origin",
                                  return_value=True) as probe:
            data = client.health()
        probe.assert_called_once_with("https://strict.example")
        self.assertTrue(data["eapi"])


class OriginRetryTest(unittest.TestCase):
    def make_client(self):
        return zlib_client.ZlibClient("https://zlib.ch/", "auto", 5)

    def test_safe_error_text_strips_query(self):
        text = zlib_client._safe_error_text(
            "GET https://x.example/a?token=abc failed at /dl/xyz?token=secret")
        self.assertNotIn("token", text)
        self.assertIn("https://x.example/a", text)
        self.assertIn("/dl/xyz", text)

    def test_probe_failure_not_cached_after_origin_switch(self):
        client = self.make_client()
        client.doorway.final_origin = "https://new.example"
        with mock.patch.object(
                client, "_eapi_json",
                side_effect=zlib_client.ZlibError("entry.unreachable", "timed out")):
            self.assertFalse(client.probe_eapi("https://old.example"))
        self.assertIsNone(client._eapi)

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
        client.doorway.final_origin = "https://old.example"
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

    def test_prepare_retry_invalidates_actual_current_origin(self):
        client = self.make_client()
        client.doorway.final_origin = "https://mid.example"
        calls = []

        def fake_once(origin, book, account=None, timeout=None):
            calls.append(origin)
            if len(calls) == 1:
                raise zlib_client.ZlibError("entry.unreachable", "Read timed out")
            return {"url": "https://cdn.example/book.epub"}

        with mock.patch.object(client, "_prepare_download_once", fake_once), \
                mock.patch.object(client, "origin",
                                  return_value="https://new.example"), \
                mock.patch.object(client.doorway, "invalidate") as invalidate:
            client.prepare_download("https://old.example", {"id": "1"})
        invalidate.assert_called_once_with("https://mid.example")

    def test_prepare_does_not_retry_404(self):
        client = self.make_client()
        calls = []

        def fake_once(origin, book, account=None, timeout=None):
            calls.append(origin)
            raise zlib_client.ZlibError("download.http", "HTTP 404")

        with mock.patch.object(client, "_prepare_download_once", fake_once), \
                mock.patch.object(client.doorway, "invalidate") as invalidate:
            with self.assertRaises(zlib_client.ZlibError):
                client.prepare_download("https://old.example", {"id": "1"})
        self.assertEqual(calls, ["https://old.example"])
        invalidate.assert_not_called()

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
            "languages": ["chinese"], "extensions": ["EPUB"],
            "year_from": 2000, "year_to": 2024, "order": "newest", "exact": True,
        })
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/eapi/book/search")
        data = captured["kwargs"]["data"]
        self.assertEqual(data["message"], "三体")
        self.assertEqual(data["page"], "2")
        self.assertEqual(data["limit"], "20")
        self.assertEqual(data["languages[]"], ["chinese"])
        self.assertEqual(data["extensions[]"], ["epub"])
        self.assertEqual(data["yearFrom"], "2000")
        self.assertEqual(data["yearTo"], "2024")
        self.assertEqual(data["order"], "newest")
        self.assertEqual(data["e"], "1")

    def test_html_search_passes_filters(self):
        client = zlib_client.ZlibClient("https://example.test/", "system", 5)
        captured = {}
        response = mock.Mock(status_code=200, text=(
            '<z-bookcard id="1" href="/book/1/x.html" extension="epub">'
            '<div slot="title">T</div></z-bookcard>'))
        client._request_challenge_aware = (
            lambda method, url, **kwargs: captured.update({"url": url}) or response)
        client.html_search("https://origin.test", "三体", 2, filters={
            "languages": ["chinese"], "extensions": ["EPUB"],
            "year_from": 2000, "year_to": 2024, "order": "newest", "exact": True,
        }, timeout=5)
        query = parse_qs(urlsplit(captured["url"]).query)
        self.assertEqual(query["page"], ["2"])
        self.assertEqual(query["languages[]"], ["chinese"])
        self.assertEqual(query["extensions[]"], ["epub"])
        self.assertEqual(query["yearFrom"], ["2000"])
        self.assertEqual(query["yearTo"], ["2024"])
        self.assertEqual(query["order"], ["newest"])
        self.assertEqual(query["e"], ["1"])

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
