"""Archive portability, separation of commands and meaningful failure states."""

import contextlib
import csv
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vk_archive import download_cli, view_cli
from vk_archive.media import DownloadStopped
from vk_archive.store import Store
from tests.test_collector import FakeAPI


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root)
        self.addCleanup(self.store.close)

    def test_readonly_view_does_not_create_an_archive(self):
        missing = self.root / "missing"
        with self.assertRaisesRegex(RuntimeError, "Архив не найден"):
            Store(missing, create=False)
        self.assertFalse(missing.exists())
        self.store.put("profile", "42", {"id": 42, "first_name": "Иван"})
        readonly = Store(self.root, create=False)
        self.addCleanup(readonly.close)
        self.assertEqual(readonly.status()["counts"]["profile"], 1)
        with self.assertRaises(sqlite3.OperationalError):
            readonly.meta("unexpected", "write")

    def test_legacy_database_is_rejected_without_migration(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        connection = sqlite3.connect(str(legacy / "archive.sqlite3"))
        connection.execute("CREATE TABLE old_data(text TEXT)")
        connection.execute("INSERT INTO old_data VALUES ('keep')")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(RuntimeError, "Неподдерживаемая версия"):
            Store(legacy)
        with sqlite3.connect(str(legacy / "archive.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT text FROM old_data").fetchone()[0], "keep")

    def test_cyrillic_search_is_case_insensitive_and_wildcards_literal(self):
        self.store.actor({"id": 50, "first_name": "Анна", "last_name": "Тест"})
        self.store.put("conversation", "50", {"conversation": {"peer": {"id": 50}}})
        self.assertEqual(self.store.objects("conversation", query="анна")["total"], 1)
        self.store.put("message", "50_1", {"id": 1, "text": "100% готово"}, parent="50")
        self.store.put("message", "50_2", {"id": 2, "text": "1000 готово"}, parent="50")
        self.assertEqual(self.store.objects("message", query="100%")["total"], 1)

    def test_search_matches_decoded_text_in_forwards_and_respects_dialog_and_year(self):
        forward = {"text": 'Привет!\nКак «дела»? 100%', "date": 1500000000}
        self.store.put("message", "50_1", {"date": 1500000000, "fwd_messages": [forward]}, parent="50")
        self.store.put("message", "50_2", {"date": 1700000000, "reply_message": forward}, parent="50")
        self.store.put("message", "60_1", forward, parent="60")
        self.store.put("message", "50_3", {"date": 1500000000, "attachments": [{"title": forward["text"]}]}, parent="50")
        result = self.store.objects("message", parent="50", query="ПРИВЕТ!   как «дела»?", after=1483228800, before=1514764800)
        self.assertEqual([row["key"] for row in result["items"]], ["50_1"])

    def test_album_includes_archived_photos_with_matching_owner_and_album(self):
        self.store.meta("owner_id", 42)
        self.store.put("photo", "42_11", {"id": 11, "owner_id": 42, "album_id": -6}, parent="attachments")
        self.store.put("photo", "51_12", {"id": 12, "owner_id": 51, "album_id": -6}, parent="attachments")
        self.store.put("photo", "42_13", {"id": 13, "owner_id": 42, "album_id": 99}, parent="album:-6")
        result = self.store.objects("photo", parent="album:-6")
        self.assertEqual({row["key"] for row in result["items"]}, {"42_11", "42_13"})

    def test_partial_file_stays_visible_and_multiple_sources_survive_hydration(self):
        key = "photo:42_71"
        self.store.asset(key, "https://cdn.example.test/a.jpg", "photo", source={"category": "dialogs", "id": "50", "name": "Анна"})
        self.store.asset(key, "https://cdn.example.test/a.jpg", "photo", source={"category": "albums", "id": "101", "name": "Лето"})
        self.store.asset(key, "https://cdn.example.test/new.jpg", "photo", source=None)
        self.assertEqual(len(self.store.media_sources(key)), 2)
        path = self.root / "media" / "photos" / "test.jpg"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"image")
        self.store.mark_media_done(key, path, 5, "testhash", "image/jpeg")
        self.store.mark_media_error(key, "partial", "Один источник не создан")
        result = self.store.bundle([{"owner_id": 42, "id": 71}])
        self.assertEqual(result["assets"][0]["local"], "/media/photo%3A42_71")
        self.assertEqual(result["assets"][0]["path"], "media/photos/test.jpg")

    def test_csv_manifest_contains_each_source_and_quotes_formulas(self):
        key = "doc:42_91"
        self.store.asset(key, "https://cdn.example.test/doc", "doc", name="=HYPERLINK(test)", source={"category": "dialogs", "id": "50", "name": "Анна"})
        self.store.asset(key, "https://cdn.example.test/doc", "doc", source={"category": "profile", "id": "42", "name": "Иван"})
        download_cli.write_report(self.store)
        with (self.root / "media_index.csv").open(encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["source"] for row in rows}, {"dialogs", "profile"})
        self.assertTrue(rows[0]["title"].startswith("'="))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = io.StringIO()

    def run_download(self, args):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return download_cli.main(["--out", str(self.root)] + args)

    def test_fatal_token_error_never_starts_media_or_viewer(self):
        def bad_api(token, store, stop, version):
            return FakeAPI(store, stop, fatal_method="users.get")
        with mock.patch.object(download_cli, "read_token", return_value="FAKE"), mock.patch.object(download_cli, "API", side_effect=bad_api), mock.patch("vk_archive.media.Downloader") as downloader, mock.patch.object(view_cli, "create_server") as server:
            self.assertEqual(self.run_download([]), 1)
        downloader.assert_not_called()
        server.assert_not_called()
        self.assertNotIn("Загрузка завершена", self.output.getvalue())

    def test_refresh_wrong_account_keeps_previous_progress(self):
        store = Store(self.root)
        store.meta("owner_id", 42)
        store.task("completed", "Готовый раздел", status="done", offset=400, received=400)
        store.close()
        def other_api(token, store, stop, version):
            return FakeAPI(store, stop, uid=999)
        with mock.patch.object(download_cli, "read_token", return_value="FAKE"), mock.patch.object(download_cli, "API", side_effect=other_api), mock.patch("vk_archive.media.Downloader") as downloader:
            self.assertEqual(self.run_download(["--refresh"]), 1)
        downloader.assert_not_called()
        reopened = Store(self.root)
        self.addCleanup(reopened.close)
        row = reopened.task("completed", "Готовый раздел")
        self.assertEqual((row["status"], row["offset"]), ("done", 400))

    def test_metadata_only_never_starts_media_or_browser(self):
        def fake_api(token, store, stop, version):
            return FakeAPI(store, stop)
        with mock.patch.object(download_cli, "read_token", return_value="FAKE"), mock.patch.object(download_cli, "API", side_effect=fake_api), mock.patch("vk_archive.media.Downloader") as downloader:
            self.assertEqual(self.run_download(["--metadata-only"]), 2)  # Fixture has a denied groups section.
        downloader.assert_not_called()
        store = Store(self.root, create=False)
        self.addCleanup(store.close)
        self.assertEqual(store.meta("run_state"), "metadata_finished")
        self.assertTrue((self.root / "report.json").is_file())
        self.assertIn("СБОР МЕТАДАННЫХ ЗАВЕРШЁН С ПРОПУСКАМИ", self.output.getvalue())

    def test_files_only_requires_successful_account_metadata(self):
        store = Store(self.root)
        store.close()
        with mock.patch.object(download_cli, "read_token") as prompt, mock.patch.object(download_cli, "API") as api:
            self.assertEqual(self.run_download(["--files-only"]), 1)
        prompt.assert_not_called()
        api.assert_not_called()

    def test_interrupted_file_download_exits_130_and_writes_report(self):
        store = Store(self.root)
        store.meta("owner_id", 42)
        store.close()
        with mock.patch("vk_archive.media.Downloader.run", side_effect=DownloadStopped()), mock.patch.object(download_cli, "read_token") as prompt:
            self.assertEqual(self.run_download(["--files-only"]), 130)
        prompt.assert_not_called()
        report = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["meta"]["run_state"], "interrupted")

    def test_visible_input_and_debug_do_not_print_token(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("builtins.input", return_value=" FAKE\x16TOKEN "), contextlib.redirect_stdout(output):
            token = download_cli.read_token(debug=True)
        self.assertEqual(token, "FAKE\x16TOKEN")
        self.assertIn("U+0016", output.getvalue())
        self.assertNotIn(token, output.getvalue())


if __name__ == "__main__":
    unittest.main()
