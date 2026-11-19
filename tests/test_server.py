"""Local HTTP integration tests: no external network or VK credentials."""

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from vk_archive.server import create_server, make_handler
from vk_archive.store import Store


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name).resolve()
        store = Store(cls.root)
        store.meta("owner_id", 42)
        store.actor({"id": 42, "first_name": "Иван", "last_name": "Тест"})
        store.actor({"id": 50, "first_name": "Анна", "last_name": "Тест"})
        store.put("profile", "42", {"id": 42, "first_name": "Иван"})
        store.put("conversation", "50", {"conversation": {"peer": {"id": 50}}, "last_message": {"from_id": 42, "date": 1700000000}})
        store.put("message", "50_1", {"id": 1, "peer_id": 50, "from_id": 50, "text": '<script>alert("test")</script> 😀', "date": 1700000000}, parent="50")
        for ident in (1, 2, 3):
            store.put("video", "42_%d" % ident, {"id": ident, "owner_id": 42}, parent="own" if ident < 3 else "attachments")
            if ident > 1:
                store.asset("video:42_%d" % ident, "", "video", source={"category": "dialogs", "id": "50"})
        store.asset("doc:42_91", "https://cdn.example.test/doc", "doc", name="Диплом.docx", source={"category": "dialogs", "id": "50", "name": "Анна"})
        path = cls.root / "media" / "documents" / "dialogs" / "Анна__50" / "Диплом.docx"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"12345")
        store.mark_media_done("doc:42_91", path.with_name("Отсутствующий.docx"), 5, hashlib.sha256(b"12345").hexdigest(), "text/html")
        store.set_source_path("doc:42_91", "dialogs", "50", path)
        store.close()
        cls.store = Store(cls.root, create=False)
        cls.server = create_server(cls.store)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.store.close()
        cls.temp.cleanup()

    def request(self, path, headers=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_separate_static_assets_and_csp_nonce(self):
        code, headers, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("nonce-", headers["Content-Security-Policy"])
        self.assertIn(b'src="/app.js"', body)
        self.assertIn(b'href="/app.css"', body)
        self.assertNotIn(b"__NONCE__", body)
        self.assertEqual(self.request("/app.js")[0], 200)
        self.assertEqual(self.request("/app.css")[0], 200)

    def test_messages_remain_json_and_casefold_name_search_works(self):
        code, headers, body = self.request("/api/objects?kind=message&parent=50")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIn("<script>", data["items"][0]["data"]["text"])
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        query = "/api/objects?kind=conversation&q=%D0%B0%D0%BD%D0%BD%D0%B0"
        self.assertEqual(json.loads(self.request(query)[2])["total"], 1)

    def test_document_download_and_ranges(self):
        code, headers, body = self.request("/media/doc%3A42_91", {"Range": "bytes=1-3"})
        self.assertEqual((code, body), (206, b"234"))
        self.assertEqual(headers["Content-Range"], "bytes 1-3/5")
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.request("/media/doc%3A42_91", {"Range": "bytes=100-"})[0], 416)
        self.assertEqual(self.request("/media/doc%3A42_91", {"Range": "bytes=-2"})[2], b"45")
        self.assertEqual(self.request("/media/doc%3A42_91", method="HEAD")[2], b"")

    def test_video_sections_filter_existing_archive_by_source(self):
        for query, keys in (("parent=own", {"42_1", "42_2"}), ("source=dialogs", {"42_2", "42_3"})):
            status, _, body = self.request("/api/objects?kind=video&" + query)
            self.assertEqual(status, 200)
            self.assertEqual({row["key"] for row in json.loads(body)["items"]}, keys)
        self.assertEqual(json.loads(self.request("/api/status?brief=1")[2])["video_counts"], {"video": 2, "dialog_video": 2})

    def test_windows_connection_abort_does_not_try_to_send_second_response(self):
        handler_type = make_handler(self.store)
        handler = handler_type.__new__(handler_type)
        handler.server = self.server
        handler.path = "/api/status"
        handler.headers = {"Host": "127.0.0.1:%d" % self.server.server_port}
        handler.send = mock.Mock(side_effect=ConnectionAbortedError(10053, "Browser disconnected"))
        handler.do_GET()
        handler.send.assert_called_once()

    def test_no_arbitrary_files_or_foreign_host(self):
        self.assertEqual(self.request("/archive.sqlite3")[0], 404)
        self.assertEqual(self.request("/media/..%2Farchive.sqlite3")[0], 404)
        self.assertEqual(self.request("/", {"Host": "attacker.invalid"})[0], 403)
        self.assertEqual(self.request("/api/objects?kind=message&limit=broken")[0], 400)


if __name__ == "__main__":
    unittest.main()
