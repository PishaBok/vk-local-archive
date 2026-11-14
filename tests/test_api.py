"""Offline transport checks: credentials, retries, raw data and redirects."""

import io
import json
import tempfile
import unittest
import urllib.parse
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path
from unittest import mock

from vk_archive.api import API, ApiError, Stopped
from vk_archive.store import Store


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value, ensure_ascii=False).encode("utf-8"))


class ScriptedOpener:
    def __init__(self, responses, repeat_last=False):
        self.responses = list(responses)
        self.repeat_last = repeat_last
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if len(self.requests) > 10:
            raise AssertionError("An API retry loop did not stop")
        if not self.responses:
            raise AssertionError("Unexpected API request")
        value = self.responses[0]
        if not self.repeat_last or len(self.responses) > 1:
            self.responses.pop(0)
        return Response(value)


class StopWithoutSleep:
    def __init__(self, stop_after=None):
        self.waits = []
        self.stop_after = stop_after

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.stop_after is not None and len(self.waits) >= self.stop_after


class TrackingHTTPS(urllib.request.HTTPSHandler):
    """A fake HTTPS wire; real sockets and DNS are never used."""

    handler_order = 100

    def __init__(self):
        super().__init__()
        self.requests = []

    def https_open(self, request):
        self.requests.append(request)
        headers = Message()
        if request.full_url.startswith("https://api.vk.com/method/"):
            headers["Location"] = "https://unrelated.example.test/login"
            result = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, code=302)
            result.msg = "Found"
        else:
            result = urllib.response.addinfourl(io.BytesIO(b'{"response": []}'), headers, request.full_url, code=200)
            result.msg = "OK"
        return result


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.close)
        self.token = "PRIVATE+TOKEN&not-a-query=value"

    def client(self, responses, repeat_last=False):
        opener = ScriptedOpener(responses, repeat_last=repeat_last)
        stop = StopWithoutSleep()
        return API(self.token, self.store, stop=stop, interval=0, opener=opener), opener, stop

    def test_token_is_post_body_not_url_and_utf8_params_round_trip(self):
        api, opener, stop = self.client([{"response": [{"id": 42, "first_name": "Анна 😀"}]}])
        result = api.call("users.get", _archive=False, fields="Имя и фото 😀")
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.vk.com/method/users.get")
        self.assertNotIn(self.token, request.full_url)
        self.assertNotIn("access_token", request.full_url)
        params = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(params["access_token"], [self.token])
        self.assertEqual(params["fields"], ["Имя и фото 😀"])
        self.assertEqual(result[0]["first_name"], "Анна 😀")
        self.assertFalse((Path(self.temp.name) / "raw").exists())

    def test_raw_json_removes_sensitive_fields_and_actual_token_text(self):
        value = {
            "text": "prefix " + self.token + " suffix",
            "access_token": self.token,
            "Authorization": "Bearer " + self.token,
            "nested": [
                {"refresh_token": "OTHER_REFRESH_SECRET"},
                {"request_params": [{"key": "access_token", "value": self.token}]},
                {"client_secret": "OTHER_CLIENT_SECRET", "text": "Сообщение 😀"},
            ],
        }
        api, opener, stop = self.client([{"response": value}])
        result = api.call("users.get", diagnostic=self.token)
        files = list((Path(self.temp.name) / "raw" / "users.get").glob("*.json"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        for secret in (self.token, "OTHER_REFRESH_SECRET", "OTHER_CLIENT_SECRET"):
            self.assertNotIn(secret, text)
        self.assertNotIn("request_params", text)
        self.assertNotIn("access_token", text)
        raw = json.loads(text)
        self.assertEqual(raw["params"]["diagnostic"], "[TOKEN]")
        self.assertEqual(raw["response"]["text"], "prefix [TOKEN] suffix")
        self.assertEqual(result["nested"][-1]["text"], "Сообщение 😀")
        self.assertTrue(raw["received_at"].endswith("Z"))
        self.assertFalse(list((Path(self.temp.name) / "raw").rglob("*.tmp")))

    def test_authentication_error_is_not_retried_or_saved_with_secrets(self):
        error = {"error": {
            "error_code": 5, "error_msg": "invalid token " + self.token,
            "request_params": [{"key": "access_token", "value": self.token}],
        }}
        api, opener, stop = self.client([error], repeat_last=True)
        with self.assertRaises(ApiError) as caught:
            api.call("users.get")
        self.assertEqual(caught.exception.code, 5)
        self.assertEqual(caught.exception.method, "users.get")
        self.assertNotIn(self.token, str(caught.exception))
        self.assertEqual(len(opener.requests), 1)
        self.assertFalse((Path(self.temp.name) / "raw").exists())

    def test_rate_limit_retries_then_returns_success_without_real_sleep(self):
        error = {"error": {"error_code": 6, "error_msg": "Too many requests"}}
        api, opener, stop = self.client([error, error, {"response": {"count": 0, "items": []}}])
        with mock.patch("vk_archive.api.time.monotonic", return_value=100.0), mock.patch("vk_archive.api.time.sleep", side_effect=AssertionError("Unexpected sleep")):
            self.assertEqual(api.call("docs.get"), {"count": 0, "items": []})
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(len([delay for delay in stop.waits if delay > 0]), 2)

    def test_persistent_rate_limit_has_a_bounded_number_of_attempts(self):
        api, opener, stop = self.client([{"error": {"error_code": 6, "error_msg": "Too many requests"}}], repeat_last=True)
        with mock.patch("vk_archive.api.time.monotonic", return_value=100.0):
            with self.assertRaises(ApiError) as caught:
                api.call("docs.get")
        self.assertEqual(caught.exception.code, 6)
        self.assertEqual(len(opener.requests), 6)
        delays = [delay for delay in stop.waits if delay > 0]
        self.assertEqual(len(delays), 5)
        self.assertTrue(all(0 < delay <= 30 for delay in delays))

    def test_interruption_during_backoff_does_not_make_another_request(self):
        error = {"error": {"error_code": 6, "error_msg": "Too many requests"}}
        api, opener, stop = self.client([error], repeat_last=True)
        stop.stop_after = 2
        with self.assertRaises(Stopped):
            api.call("docs.get")
        self.assertEqual(len(opener.requests), 1)

    def test_auth_redirect_is_never_followed(self):
        stop = StopWithoutSleep()
        api = API(self.token, self.store, stop=stop, interval=0)
        wire = TrackingHTTPS()
        api.opener.add_handler(wire)
        with mock.patch("vk_archive.api.time.monotonic", return_value=100.0):
            with self.assertRaises(RuntimeError):
                api.call("users.get")
        self.assertGreater(len(wire.requests), 0)
        self.assertLessEqual(len(wire.requests), 6)
        self.assertTrue(all(request.full_url == "https://api.vk.com/method/users.get" for request in wire.requests))


if __name__ == "__main__":
    unittest.main()
