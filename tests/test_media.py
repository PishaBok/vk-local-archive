import base64
import hashlib
import http.client
import io
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
from email.message import Message
from pathlib import Path, PureWindowsPath
from unittest import mock

from vk_archive import media
from vk_archive.store import Store


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j0XsAAAAASUVORK5CYII=")


class Response(io.BytesIO):
    def __init__(self, value, content_type="application/octet-stream", length=None):
        super().__init__(value)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(value) if length is None else length)


class PathLayoutTests(unittest.TestCase):
    def test_readable_paths_work_as_windows_paths(self):
        source = {"category": "dialogs", "id": 2000000001, "name": 'Друзья: диплом / поездка? *', "date": 1700000000}
        path = media.plan_path("doc", source, key="doc:42_91", title='Диплом: финал?.docx', ext="docx", timestamp=1700000000)
        self.assertEqual(path.parts[:3], ("media", "documents", "dialogs"))
        self.assertIn("Друзья", path.parts[3])
        self.assertIn("2000000001", path.parts[3])
        self.assertIn("Диплом", path.name)
        self.assertIn("2023-11-14", path.name)
        self.assertIn("vk_42_91", path.name)
        self.assertTrue(path.name.endswith(".docx"))
        self.assertFalse(PureWindowsPath(path.as_posix()).is_absolute())
        for component in path.parts:
            self.assertFalse(re.search(r'[<>:"/\\|?*\x00-\x1f]', component))
            self.assertFalse(component.endswith((".", " ")))
            self.assertLessEqual(len(component.encode("utf-8")), 255)

    def test_windows_device_names_and_hidden_controls(self):
        for name in ("CON", "nul.txt", "COM1", "LPT³.log", "AUX.", "PRN "):
            with self.subTest(name=name):
                actual = media.safe_component(name)
                self.assertNotIn(actual.split(".")[0].upper(), media.WINDOWS_RESERVED)
        self.assertEqual(media.safe_component("Отчёт\u202e\u200b\x00.docx"), "Отчёт.docx")
        self.assertEqual(media.safe_component(".."), "unnamed")

    def test_long_names_remain_distinct_and_keep_stable_ids(self):
        source = {"category": "albums", "id": "42_101", "name": "Очень длинное название альбома 😀 " * 50}
        first = media.plan_path("photo", source, key="photo:42_71", title="Фото " * 100, timestamp=1700000000, compact=True)
        second = media.plan_path("photo", source, key="photo:42_72", title="Фото " * 100, timestamp=1700000000, compact=True)
        self.assertNotEqual(first, second)
        self.assertIn("42_101", first.parts[3])
        self.assertIn("vk_42_71", first.name)
        self.assertLess(len(first.as_posix().encode("utf-16-le")) // 2, 190)
        source["id"] = "-123456789_1234567890"
        long_id = media.plan_path("photo", source, key="photo:42_71", title="Фото", compact=True)
        self.assertIn(source["id"], long_id.parts[3])

    def test_extension_and_largest_picture(self):
        self.assertEqual(media.choose_extension("doc", "Документ.docx", mime="application/zip"), ".docx")
        self.assertEqual(media.choose_extension("photo", ext="jpg", mime="image/webp"), ".webp")
        self.assertEqual(media.choose_picture({"image": [{"url": "small", "width": 10, "height": 10}, {"url": "large", "width": 20, "height": 20}]}), "large")
        self.assertEqual(media.choose_picture({"thumb_256": "https://example.test/gift.png"}), "https://example.test/gift.png")


class DirectDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.guard = mock.patch.object(media, "public_https", side_effect=lambda value: value)
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def download(self, response, kind="photo", name="photo.jpg", **kwargs):
        opener = mock.Mock()
        opener.open.return_value = response
        result = media.download_file("https://cdn.example.test/media", self.root / name, kind, opener=opener, **kwargs)
        request = opener.open.call_args[0][0]
        self.assertIsNone(request.data)
        self.assertNotIn("Authorization", dict(request.header_items()))
        self.assertNotIn("Cookie", dict(request.header_items()))
        return result

    def test_magic_mime_and_atomic_file(self):
        result = self.download(Response(PNG))
        self.assertEqual(result.path.suffix, ".png")
        self.assertEqual(result.path.read_bytes(), PNG)
        self.assertEqual(result.sha256, hashlib.sha256(PNG).hexdigest())
        self.assertEqual(result.size, len(PNG))
        self.assertFalse(list(self.root.glob("*.part")))
        self.assertFalse(list(self.root.glob(".*.part")))

    def test_html_error_is_not_saved_as_document(self):
        with self.assertRaisesRegex(media.MediaDownloadError, "HTML"):
            self.download(Response(b"<html>Login</html>", "text/html"), kind="doc", name="Diploma.docx", expected_ext="docx")
        self.assertFalse((self.root / "Diploma.docx").exists())

    def test_document_size_and_full_response_length(self):
        with self.assertRaisesRegex(media.MediaDownloadError, "Размер документа"):
            self.download(Response(b"12345"), kind="doc", name="Diploma.docx", expected_size=6)
        with self.assertRaisesRegex(media.MediaDownloadError, "не полностью"):
            self.download(Response(PNG, length=len(PNG) + 1))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unknown_document_extension_uses_reported_mime(self):
        value = b"%PDF-1.7\nexample"
        result = self.download(Response(value, "application/pdf"), kind="doc", name="Документ.bin")
        self.assertEqual(result.path.suffix, ".pdf")

    def test_json_error_and_hls_are_not_ready_media(self):
        for response in (Response(b'{"error":"expired"}', "application/json"), Response(b"#EXTM3U\nhttps://cdn.example.test/segment.ts", "application/octet-stream")):
            with self.subTest(response=response), self.assertRaises(media.MediaDownloadError):
                self.download(response)

    def test_video_download_rejects_an_image_under_mp4_url(self):
        with self.assertRaisesRegex(media.MediaDownloadError, "другого типа"):
            self.download(Response(PNG), kind="video", name="Video.mp4")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_http_and_nested_network_errors_are_actionable_without_leaking_urls(self):
        private_url = "https://cdn.example.test/file?token=PRIVATE"
        openssl_failure = ssl.SSLCertVerificationError(1, private_url)
        openssl_failure.verify_code = 20
        openssl_failure.verify_message = private_url
        windows_failure = ssl.SSLCertVerificationError(private_url)
        windows_failure.verify_code = 0x800B0109
        windows_failure.verify_message = private_url
        cases = (
            (urllib.error.HTTPError(private_url, 403, private_url, {}, None), "HTTP 403.*--refresh", False),
            (urllib.error.URLError(ssl.SSLCertVerificationError(1, private_url)), "сертификат HTTPS", False),
            (urllib.error.URLError(openssl_failure), "код 20.*доверенную цепочку", False),
            (urllib.error.URLError(windows_failure), "код 0x800B0109.*корневому сертификату", False),
            (urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, private_url)), "DNS", True),
            (urllib.error.URLError(TimeoutError(private_url)), "время ожидания", True),
            (urllib.error.URLError(OSError("Tunnel connection failed: 407 " + private_url)), "Прокси.*HTTP 407", False),
            (http.client.IncompleteRead(private_url.encode()), "полного файла", True),
        )
        for error, message, retry in cases:
            with self.subTest(error=type(error).__name__):
                opener = mock.Mock()
                opener.open.side_effect = error
                with self.assertRaisesRegex(media.MediaDownloadError, message) as raised:
                    media.download_file(private_url, self.root / "Video.mp4", "video", opener=opener)
                self.assertEqual(raised.exception.retryable, retry)
                self.assertNotIn("PRIVATE", str(raised.exception))
                self.assertNotIn("cdn.example.test", str(raised.exception))

    def test_system_tls_uses_a_verified_context_and_keeps_redirect_checks(self):
        truststore = types.SimpleNamespace(SSLContext=mock.Mock(side_effect=ssl.SSLContext))
        with mock.patch.dict(sys.modules, {"truststore": truststore}):
            opener = media.media_opener(system_tls=True)
        truststore.SSLContext.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        handler = next(handler for handler in opener.handlers if isinstance(handler, media.urllib.request.HTTPSHandler))
        self.assertTrue(handler._context.check_hostname)
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(any(isinstance(handler, media.SafeRedirect) for handler in opener.handlers))

    def test_truncated_network_stream_leaves_no_file(self):
        class TruncatedResponse(Response):
            def read(self, size):
                if size == 8192:
                    return super().read(size)
                raise http.client.IncompleteRead(b"partial")

        with self.assertRaisesRegex(media.MediaDownloadError, "полного файла"):
            self.download(TruncatedResponse(PNG))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_successful_partial_http_response_is_not_marked_as_complete(self):
        response = Response(PNG)
        response.status = 206
        response.headers["Content-Range"] = "bytes 0-%s/%s" % (len(PNG) - 1, len(PNG) + 500)
        with self.assertRaisesRegex(media.MediaDownloadError, "только часть файла"):
            self.download(response)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_stop_leaves_no_partial_file(self):
        stop = threading.Event()
        class StoppingResponse(Response):
            def read(self, *args):
                value = super().read(*args)
                stop.set()
                return value
        with self.assertRaises(media.DownloadStopped):
            self.download(StoppingResponse(PNG), stop=stop)
        self.assertEqual(list(self.root.iterdir()), [])


class SourceReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.close)
        self.stop = threading.Event()
        self.asset_key = "photo:42_71"
        self.store.asset(self.asset_key, "https://cdn.example.test/photo", "photo", "Поездка", source={"category": "dialogs", "id": 50, "name": "Анна", "date": 1700000100}, metadata={"date": 1700000000})
        self.store.asset(self.asset_key, "https://cdn.example.test/photo", "photo", "Поездка", source={"category": "albums", "id": "42_101", "name": "Лето", "date": 1700000000})

    def fake_download(self, url, destination, kind, **kwargs):
        path = destination.with_suffix(".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        return media.DownloadResult(path.resolve(), len(PNG), hashlib.sha256(PNG).hexdigest(), "image/png")

    def test_one_download_all_sources_and_no_network_on_resume(self):
        with mock.patch.object(media, "download_file", side_effect=self.fake_download) as download:
            first = media.Downloader(self.store, self.stop).run()
            self.assertEqual(download.call_count, 1)
            second = media.Downloader(self.store, self.stop).run()
            self.assertEqual(download.call_count, 1)
        self.assertEqual(first["downloaded"], 1)
        self.assertEqual(second["reused"], 1)
        row = self.store.get_media(self.asset_key)
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["path"].startswith("media/photos/albums/"))
        sources = self.store.media_sources(self.asset_key)
        self.assertEqual(len(sources), 2)
        for source in sources:
            self.assertTrue((self.store.root / source["path"]).is_file())
            self.assertEqual((self.store.root / source["path"]).read_bytes(), PNG)
        self.assertEqual(media.verify_files(self.store)["errors"], [])

    def test_copy_fallback_and_missing_canonical_repair(self):
        with mock.patch.object(media.os, "link", side_effect=OSError("not supported")), mock.patch.object(media, "download_file", side_effect=self.fake_download) as download:
            media.Downloader(self.store, self.stop).run()
            canonical = self.store.root / self.store.get_media(self.asset_key)["path"]
            canonical.unlink()
            self.assertTrue(media.verify_files(self.store)["errors"])
            media.Downloader(self.store, self.stop).run()
            self.assertEqual(download.call_count, 1)
            self.assertTrue(canonical.is_file())
        self.assertEqual(media.verify_files(self.store)["errors"], [])

    def test_reference_failure_is_visible_as_partial(self):
        original = media.ensure_reference
        def fail_one(source, destination, stop=None):
            if "dialogs" in destination.parts:
                raise PermissionError("test")
            return original(source, destination, stop=stop)
        with mock.patch.object(media, "download_file", side_effect=self.fake_download), mock.patch.object(media, "ensure_reference", side_effect=fail_one), mock.patch.object(self.store, "log") as log:
            summary = media.Downloader(self.store, self.stop).run()
        row = self.store.get_media(self.asset_key)
        self.assertEqual(row["status"], "partial")
        self.assertTrue((self.store.root / row["path"]).is_file())
        self.assertIn("dialogs/50", row["error"])
        self.assertEqual(summary["partial"], 1)
        log.assert_called_once()
        self.assertTrue(any(x["source"] == "dialogs/50" for x in media.verify_files(self.store)["errors"]))

    def test_repeated_url_uses_existing_bytes_in_other_layout(self):
        self.store.asset("photo:42_72", "https://cdn.example.test/photo", "photo", "Другое фото", source={"category": "bookmarks", "id": 42, "name": "Закладки", "date": 1700000000})
        with mock.patch.object(media, "download_file", side_effect=self.fake_download) as download:
            summary = media.Downloader(self.store, self.stop).run()
        self.assertEqual(download.call_count, 1)
        self.assertEqual(summary["reused"], 1)
        other = self.store.get_media("photo:42_72")
        self.assertIn("bookmarks", other["path"])
        self.assertEqual((self.store.root / other["path"]).read_bytes(), PNG)

    def test_verify_detects_same_length_changed_reference(self):
        with mock.patch.object(media.os, "link", side_effect=OSError("not supported")), mock.patch.object(media, "download_file", side_effect=self.fake_download):
            media.Downloader(self.store, self.stop).run()
        source = next(x for x in self.store.media_sources(self.asset_key) if x["category"] == "dialogs")
        (self.store.root / source["path"]).write_bytes(b"X" * len(PNG))
        result = media.verify_files(self.store)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["source"], "dialogs/50")
        self.assertEqual(json.loads((self.store.root / "verify_report.json").read_text())["errors"], result["errors"])

    def test_interrupt_after_download_keeps_canonical_for_resume(self):
        original = self.fake_download
        def completed_but_stopped(*args, **kwargs):
            result = original(*args, **kwargs)
            self.stop.set()
            return result
        with mock.patch.object(media, "download_file", side_effect=completed_but_stopped) as download:
            with self.assertRaises(media.DownloadStopped):
                media.Downloader(self.store, self.stop).run()
            row = self.store.get_media(self.asset_key)
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["sha256"], hashlib.sha256(PNG).hexdigest())
            self.assertTrue((self.store.root / row["path"]).is_file())
            self.stop.clear()
            media.Downloader(self.store, self.stop).run()
            self.assertEqual(download.call_count, 1)
        self.assertEqual(self.store.get_media(self.asset_key)["status"], "done")
        self.assertEqual(media.verify_files(self.store)["errors"], [])

    def test_interrupt_during_reference_creation_repairs_without_download(self):
        original = media.ensure_reference
        def interrupted_reference(source, destination, stop=None):
            if "dialogs" in destination.parts:
                self.stop.set()
                raise media.DownloadStopped()
            return original(source, destination, stop=stop)
        with mock.patch.object(media, "download_file", side_effect=self.fake_download) as download:
            with mock.patch.object(media, "ensure_reference", side_effect=interrupted_reference):
                with self.assertRaises(media.DownloadStopped):
                    media.Downloader(self.store, self.stop).run()
            row = self.store.get_media(self.asset_key)
            self.assertEqual(row["status"], "partial")
            self.assertTrue((self.store.root / row["path"]).is_file())
            self.stop.clear()
            media.Downloader(self.store, self.stop).run()
            self.assertEqual(download.call_count, 1)
        self.assertEqual(media.verify_files(self.store)["errors"], [])

    def test_progress_callback_runs_before_each_asset(self):
        seen = []
        def progress(index, total, row):
            seen.append((index, total, row["key"]))
        with mock.patch.object(media, "download_file", side_effect=self.fake_download):
            media.Downloader(self.store, self.stop, progress=progress).run()
        self.assertEqual(seen, [(1, 1, self.asset_key)])

    def test_copy_fallback_can_be_stopped_without_partial_destination(self):
        source = self.store.root / "large.bin"
        source.write_bytes(b"X" * (1024 * 1024))
        destination = self.store.root / "media" / "copy.bin"
        class StopDuringCopy:
            calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls >= 3
        with mock.patch.object(media.os, "link", side_effect=OSError("not supported")):
            with self.assertRaises(media.DownloadStopped):
                media.ensure_reference(source, destination, stop=StopDuringCopy())
        self.assertFalse(destination.exists())
        self.assertEqual(source.stat().st_size, 1024 * 1024)
        self.assertEqual(list(destination.parent.iterdir()), [])


class URLValidationTests(unittest.TestCase):
    def test_private_addresses_and_credentials_are_rejected_without_request(self):
        for url in ("http://example.test/a", "https://user:pass@example.test/a", "https://example.test:8443/a"):
            with self.subTest(url=url), self.assertRaises(media.MediaDownloadError):
                media.public_https(url)
        with mock.patch.object(media.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(media.MediaDownloadError):
                media.public_https("https://example.test/a")


class VideoRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.close)
        self.worker = media.Downloader(self.store, threading.Event())
        self.key = "video:42_3"
        self.high = "https://example.test/high.mp4?token=PRIVATE"
        self.low = "https://example.test/low.mp4?token=PRIVATE"
        self.store.asset(self.key, self.high, "video", "Ролик", metadata={"files": {
            "mp4_1080": self.high, "mp4_720": self.high, "mp4_360": self.low, "external": "https://example.test/player",
        }})

    def test_previously_failed_video_recovers_with_alternate_saved_url(self):
        self.store.mark_media_error(self.key, "failed", "Сетевая ошибка при скачивании: URLError")
        data = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42"
        def load(request, **kwargs):
            if request.full_url == self.high:
                raise urllib.error.HTTPError(self.high, 403, "expired", {}, None)
            self.assertEqual(request.full_url, self.low)
            return Response(data)
        with mock.patch.object(media, "public_https", side_effect=lambda url: url), mock.patch.object(self.worker.opener, "open", side_effect=load) as opener:
            summary = self.worker.run()
            self.assertEqual(self.worker.run()["reused"], 1)
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(opener.call_count, 2)
        row = self.store.get_media(self.key)
        self.assertEqual(row["mime"], "video/mp4")
        self.assertEqual(row["status"], "done")
        self.assertEqual((self.store.root / row["path"]).read_bytes(), data)

    def test_all_alternates_fail_with_resolution_and_http_status(self):
        def load(request, **kwargs):
            status = 403 if request.full_url == self.high else 404
            raise urllib.error.HTTPError(request.full_url, status, "unavailable", {}, None)
        with mock.patch.object(media, "public_https", side_effect=lambda url: url), mock.patch.object(self.worker.opener, "open", side_effect=load):
            summary = self.worker.run()
        self.assertEqual(summary["failed"], 1)
        error = self.store.get_media(self.key)["error"]
        self.assertIn("1080p: Сервер файла вернул HTTP 403", error)
        self.assertIn("360p: Сервер файла вернул HTTP 404", error)
        self.assertNotIn("PRIVATE", error)


if __name__ == "__main__":
    unittest.main()
