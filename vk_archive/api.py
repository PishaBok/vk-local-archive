"""Small read-only VK API client; credentials never enter saved responses."""

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__


class ApiError(Exception):
    def __init__(self, method, code, message):
        self.method = method
        self.code = int(code)
        self.message = str(message)
        super().__init__("%s: VK %s — %s" % (method, self.code, message))


class Stopped(Exception):
    """The user interrupted the export; committed pages remain resumable."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, token, store, stop=None, version="5.199", interval=0.4, opener=None):
        self.token = token
        self.store = store
        self.stop = stop or threading.Event()
        self.version = version
        self.interval = interval
        self.last = 0.0
        self.opener = opener or urllib.request.build_opener(_NoRedirect())

    def clean(self, value):
        if isinstance(value, dict):
            return {key: self.clean(item) for key, item in value.items() if key.lower() not in {
                "access_token", "refresh_token", "client_secret", "authorization", "request_params"
            }}
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, str) and self.token:
            return value.replace(self.token, "[TOKEN]")
        return value

    def call(self, method, _archive=True, **params):
        if not re.fullmatch(r"[A-Za-z]+\.[A-Za-z]+", method):
            raise ValueError("Недопустимое имя метода API")
        for attempt in range(6):
            if self.stop.wait(max(0, self.interval - (time.monotonic() - self.last))):
                raise Stopped()
            body = urllib.parse.urlencode(dict(params, access_token=self.token, v=self.version)).encode("utf-8")
            request = urllib.request.Request(
                "https://api.vk.com/method/" + method,
                data=body,
                headers={"User-Agent": "LocalVKArchive/" + __version__},
            )
            self.last = time.monotonic()
            try:
                with self.opener.open(request, timeout=45) as response:
                    data = response.read(64 * 1024 * 1024 + 1)
                if len(data) > 64 * 1024 * 1024:
                    raise ValueError("Слишком большой ответ API")
                result = json.loads(data)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if attempt == 5:
                    raise RuntimeError("%s: сетевая ошибка или некорректный ответ (%s)" % (method, type(exc).__name__)) from None
                if self.stop.wait(min(2 ** attempt, 20)):
                    raise Stopped()
                continue
            if not isinstance(result, dict):
                raise RuntimeError(method + ": неожиданный формат ответа API")
            if "error" in result:
                error = result["error"]
                code = int(error.get("error_code", -1))
                if code in (1, 6, 9, 10, 29) and attempt < 5:
                    if self.stop.wait(min(2 ** attempt + 1, 30)):
                        raise Stopped()
                    continue
                raise ApiError(method, code, self.clean(str(error.get("error_msg", "Ошибка API"))))
            if "response" not in result:
                raise RuntimeError(method + ": отсутствует response")
            value = self.clean(result["response"])
            if _archive:
                self.save_raw(method, params, value)
            return value
        raise RuntimeError(method + ": число попыток исчерпано")

    def save_raw(self, method, params, data):
        params = self.clean(params)
        encoded = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256((self.version + encoded).encode("utf-8")).hexdigest()[:24]
        folder = self.store.root / "raw" / method
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (fingerprint + ".json")
        temporary = target.with_suffix(".tmp")
        value = {"method": method, "params": params, "version": self.version, "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "response": self.clean(data)}
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(target))
