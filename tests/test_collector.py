"""Collector integration tests use synthetic JSON and never contact VK."""

import contextlib
import copy
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from vk_archive.api import API, ApiError, Stopped
from vk_archive.collector import Collector, source_context
from vk_archive.store import Store


PHOTO = {
    "id": 71, "owner_id": 42, "album_id": 101, "date": 1700000000,
    "sizes": [{"width": 1280, "height": 720, "url": "https://cdn.example.test/photo.jpg"}],
}
DOCUMENT = {
    "id": 91, "owner_id": 42, "date": 1700000001, "title": "Диплом.docx",
    "ext": "docx", "size": 5, "url": "https://cdn.example.test/document.docx",
}
VIDEO = {
    "id": 81, "owner_id": 42, "date": 1700000002, "title": "Мой ролик", "duration": 10,
    "player": "https://video.example.test/player", "access_key": "test-access-key",
    "image": [{"url": "https://cdn.example.test/video.jpg", "width": 640, "height": 360}],
}


class FakeAPI(API):
    def __init__(self, store, stop=None, uid=42, fatal_method=None):
        super().__init__("FAKE_SECRET_TOKEN", store, stop=stop, interval=0)
        self.uid = uid
        self.calls = []
        self.fatal_method = fatal_method

    def call(self, method, _archive=True, **params):
        self.calls.append((method, params))
        if method == self.fatal_method:
            raise ApiError(method, 5, "invalid access_token")
        offset, count = params.get("offset", 0), params.get("count", 100)
        if method == "users.get":
            ids = [int(x) for x in params["user_ids"].split(",")] if "user_ids" in params else [self.uid]
            data = [{"id": ident, "first_name": "Владелец" if ident == 42 else "Собеседник", "last_name": "Тест",
                     "photo_max_orig": "https://cdn.example.test/avatar_%s.jpg" % ident} for ident in ids]
        elif method == "account.getProfileInfo":
            data = {"first_name": "Владелец", "last_name": "Тест"}
        elif method == "account.getAppPermissions":
            data = 4096 + 4 + 131072
        elif method == "messages.getConversations":
            items = [{"conversation": {"peer": {"id": 50, "type": "user"}},
                      "last_message": {"date": 1700000000, "from_id": 42, "text": "Привет"}}]
            data = {"count": len(items), "items": items[offset:offset + count]}
        elif method == "messages.getHistory":
            items = [{"id": i + 1, "conversation_message_id": i + 1, "peer_id": 50, "from_id": 50,
                      "date": 1700000000 + i, "text": 'Текст <script>alert("x")</script> 😀 №' + str(i)} for i in range(202)]
            items[0]["attachments"] = [
                {"type": "photo", "photo": PHOTO}, {"type": "doc", "doc": DOCUMENT},
                {"type": "video", "video": VIDEO},
                {"type": "audio_message", "audio_message": {"id": 11, "owner_id": 50, "duration": 1, "link_mp3": "https://cdn.example.test/voice.mp3"}},
                {"type": "audio", "audio": {"id": 12, "owner_id": 50, "artist": "Автор", "title": "Музыка", "duration": 1, "url": "https://cdn.example.test/music.mp3"}},
            ]
            items[0]["reply_message"] = {"id": 77, "from_id": 77, "date": 1600000000, "text": "Ответ"}
            items[0]["fwd_messages"] = [{"id": 78, "from_id": 78, "date": 1600000000, "text": "Пересылка"}]
            items[0]["action"] = {"type": "chat_invite_user", "member_id": 79}
            data = {"count": len(items), "items": items[offset:offset + count]}
        elif method == "photos.getAlbums":
            items = [{"id": 101, "owner_id": 42, "title": "Лето / отпуск"}]
            data = {"count": 1, "items": items[offset:offset + count]}
        elif method == "photos.get":
            items = [PHOTO] if params["album_id"] in ("101", "saved") else []
            data = {"count": len(items), "items": items[offset:offset + count]}
        elif method == "docs.get":
            data = {"count": 1, "items": [DOCUMENT][offset:offset + count]}
        elif method == "wall.get":
            items = [{"id": 1, "owner_id": 42, "from_id": 42, "date": 1700000010, "text": "Запись на стене",
                      "attachments": [{"type": "photo", "photo": PHOTO}], "comments": {"count": 12}}]
            data = {"count": 1, "items": items[offset:offset + count]}
        elif method == "wall.getComments":
            replies = [{"id": i + 2, "from_id": 50, "date": 1700000020 + i, "text": "Ответ " + str(i), "reply_to_comment": 1} for i in range(11)]
            items = replies if params.get("comment_id") else [{"id": 1, "from_id": 50, "date": 1700000011, "text": "Комментарий", "thread": {"count": 11, "items": replies[:10]}}]
            data = {"count": len(items), "items": items[offset:offset + count]}
        elif method == "friends.get":
            data = {"count": 1, "items": [{"id": 50, "first_name": "Анна", "last_name": "Тест"}][offset:offset + count]}
        elif method == "groups.get":
            raise ApiError(method, 7, "Нет прав")
        elif method == "groups.getById":
            data = {"groups": [{"id": int(x), "name": "Сообщество", "type": "group"} for x in params["group_ids"].split(",")]}
        elif method == "gifts.get":
            data = {"count": 1, "items": [{"id": 5, "from_id": 80, "date": 1700000000, "message": "Подарок", "gift": {"id": 7, "thumb_256": "https://cdn.example.test/gift.png"}}][offset:offset + count]}
        elif method == "fave.get":
            data = {"count": 1, "items": [{"type": "post", "added_date": 1700000030,
                    "post": {"id": 2, "owner_id": -99, "from_id": -99, "date": 1700000000, "text": "Закладка",
                             "attachments": [{"type": "photo", "photo": PHOTO}]}}][offset:offset + count]}
        elif method == "fave.getPages":
            data = {"count": 1, "items": [{"type": "group", "group": {"id": 99, "name": "Сообщество", "type": "group"}}][offset:offset + count]}
        elif method == "users.getSubscriptions":
            data = {"count": 1, "items": [{"id": 99, "type": "group", "name": "Сообщество"}][offset:offset + count]}
        elif method == "users.getFollowers":
            data = {"count": 0, "items": []}
        elif method == "video.get":
            items = [dict(VIDEO, files={"mp4_720": "https://cdn.example.test/video.mp4"})] if "videos" in params else [VIDEO]
            data = {"count": len(items), "items": items[offset:offset + count]}
        else:
            raise AssertionError("Неожиданный метод " + method)
        data = copy.deepcopy(data)
        if _archive:
            self.save_raw(method, params, data)
        return data


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.close)
        self.stop = threading.Event()

    def collect(self, api=None):
        api = api or FakeAPI(self.store, self.stop)
        with contextlib.redirect_stdout(io.StringIO()):
            result = Collector(api, self.store, self.stop).run()
        return result, api

    def test_sections_pagination_dedup_and_complete_comment_threads(self):
        result, api = self.collect()
        self.assertTrue(result)
        self.assertEqual(self.store.meta("run_state"), "api_finished")
        counts = self.store.status()["counts"]
        self.assertEqual(counts["message"], 202)
        self.assertEqual(counts["conversation"], 1)
        self.assertEqual(counts["photo"], 1)
        self.assertEqual(counts["comment"], 24)
        self.assertTrue(any(method == "messages.getHistory" and params.get("offset") == 200 for method, params in api.calls))
        self.assertTrue(any(method == "wall.getComments" and params.get("comment_id") == 1 for method, params in api.calls))
        self.assertFalse(any(method == "photos.getAll" for method, _ in api.calls))
        tasks = self.store.status()["tasks"]
        self.assertTrue(any(row["status"] == "error" and row["label"] == "Сообщества" for row in tasks))
        self.assertTrue(any(row["status"] == "unsupported" and row["label"] == "Музыкальная коллекция" for row in tasks))
        raw = "\n".join(path.read_text(encoding="utf-8") for path in Path(self.temp.name).rglob("*.json"))
        self.assertNotIn("FAKE_SECRET_TOKEN", raw)

    def test_media_retains_multiple_named_sources(self):
        self.collect()
        sources = self.store.media_sources("photo:42_71")
        self.assertTrue({"dialogs", "albums", "wall", "bookmarks"}.issubset({row["category"] for row in sources}))
        dialog = next(row for row in sources if row["category"] == "dialogs")
        self.assertEqual(dialog["source_id"], "50")
        self.assertEqual(dialog["name"], "Анна Тест")
        self.assertEqual(dialog["ts"], 1700000000)
        album = next(row for row in sources if row["source_id"] == "101")
        self.assertEqual(album["name"], "Лето / отпуск")

    def test_resume_recovers_actor_ids_from_completed_pages(self):
        self.collect()
        self.store.execute("DELETE FROM actors WHERE id IN (77,78,79)")
        result, api = self.collect()
        self.assertTrue(result)
        self.assertFalse(any(method == "messages.getHistory" for method, params in api.calls))
        ids = {row["id"] for row in self.store.rows("SELECT id FROM actors")}
        self.assertTrue({77, 78, 79}.issubset(ids))
        self.assertEqual(self.store.status()["counts"]["message"], 202)

    def test_foreign_token_does_not_overwrite_raw_profile(self):
        self.collect()
        folder = Path(self.temp.name) / "raw" / "users.get"
        before = {path.name: path.read_bytes() for path in folder.glob("*.json")}
        result, api = self.collect(FakeAPI(self.store, self.stop, uid=999))
        self.assertFalse(result)
        self.assertEqual(self.store.meta("owner_id"), 42)
        self.assertEqual(before, {path.name: path.read_bytes() for path in folder.glob("*.json")})

    def test_fatal_authorization_failure_stops_following_sections(self):
        result, api = self.collect(FakeAPI(self.store, self.stop, fatal_method="messages.getHistory"))
        self.assertFalse(result)
        self.assertEqual(self.store.meta("run_state"), "error")
        self.assertFalse(any(method == "wall.get" for method, params in api.calls))

    def test_video_hydration_updates_url_without_unknown_source(self):
        self.collect()
        media = self.store.get_media("video:42_81")
        self.assertEqual(media["url"], "https://cdn.example.test/video.mp4")
        self.assertNotIn("unknown", {row["category"] for row in self.store.media_sources("video:42_81")})

    def test_changed_avatar_keeps_distinct_media_snapshots(self):
        collector = Collector(FakeAPI(self.store, self.stop), self.store, self.stop)
        first = {"id": 50, "first_name": "Анна", "photo_200": "https://cdn.example.test/old.jpg"}
        second = dict(first, photo_200="https://cdn.example.test/new.jpg")
        collector.register_actor(first)
        collector.register_actor(second)
        rows = self.store.rows("SELECT key,url FROM assets WHERE kind='avatar'")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["key"], rows[1]["key"])

    def test_system_alias_reuses_server_returned_album_source_and_parent(self):
        collector = Collector(FakeAPI(self.store, self.stop), self.store, self.stop)
        collector.uid = 42
        photo = dict(PHOTO, album_id=-77)

        def call(method, **params):
            if method == "photos.getAlbums":
                return {"count": 1, "items": [{"id": -77, "owner_id": 42, "title": "Системный альбом"}][params["offset"]:]}
            self.assertEqual(method, "photos.get")
            self.assertNotEqual(params["album_id"], "-77")
            items = [photo] if params["album_id"] == "saved" else []
            return {"count": len(items), "items": items[params["offset"]:]}

        collector.api.call = call
        with contextlib.redirect_stdout(io.StringIO()):
            collector.photos()
        sources = self.store.media_sources("photo:42_71")
        self.assertEqual([row["source_id"] for row in sources], ["-77"])
        parents = {row["parent"] for row in self.store.rows("SELECT parent FROM links WHERE kind='photo' AND key='42_71'")}
        self.assertIn("album:-77", parents)
        self.assertIn("album:saved", parents)

    def test_bulk_fallback_recovers_album_photos_and_reports_missing_remainder(self):
        collector = Collector(FakeAPI(self.store, self.stop), self.store, self.stop)
        collector.uid = 42
        thumb = "https://cdn.example.test/cover.jpg"
        photo = dict(PHOTO, album_id=-77)

        def call(method, **params):
            if method == "photos.getAlbums":
                return {"count": 1, "items": [{"id": -77, "owner_id": 42, "title": "Системный", "size": 2, "thumb_src": thumb}]}
            if method == "photos.get":
                return {"count": 0, "items": []}
            self.assertEqual(method, "photos.getAll")
            self.assertEqual(params["no_service_albums"], 0)
            return {"count": 2, "items": [photo, dict(photo, owner_id=99, id=72)]}

        collector.api.call = call
        with contextlib.redirect_stdout(io.StringIO()):
            collector.photos()
        self.assertEqual(self.store.objects("photo", parent="album:-77")["total"], 1)
        self.assertEqual(self.store.objects("photo", parent="photos:own")["total"], 1)
        self.assertIsNone(self.store.get_media("photo:99_72"))
        self.assertEqual(self.store.rows("SELECT kind FROM assets WHERE url=?", (thumb,))[0]["kind"], "preview")
        task = self.store.task("photos.coverage:-77", "Системный")
        self.assertEqual((task["status"], task["received"], task["expected"]), ("partial", 1, 2))

    def test_protected_or_missing_direct_video_urls_are_rechecked_in_one_batch(self):
        collector = Collector(FakeAPI(self.store, self.stop), self.store, self.stop)
        source = source_context("dialogs", 50, "Тест")
        expired = dict(VIDEO, files={"mp4_720": "https://cdn.example.test/expired.mp4"})
        collector.attachment("video", expired, source)
        collector.attachment("video", dict(VIDEO, id=82, files={"external": "https://player.example.test/"}), source)
        self.store.mark_media_error("video:42_81", "failed", "Истекла ссылка")
        self.store.task("video.details:42_81", "Видео", status="done")
        collector.attachment("video", dict(expired, id=83), source)
        self.store.mark_media_done("video:42_83", "media/ready.mp4", 1, "hash", "video/mp4")
        fresh = "https://cdn.example.test/fresh.mp4"
        requests = []

        def call(method, **params):
            requests.append((method, params))
            # Only one of the two requested videos is available to this token.
            return {"items": [dict(VIDEO, files={"mp4_360": fresh})]}

        collector.api.call = call
        with contextlib.redirect_stdout(io.StringIO()):
            collector.refresh_media_urls("video")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][1]["videos"], "42_81_test-access-key,42_82_test-access-key")
        self.assertEqual(self.store.get_media("video:42_81")["url"], fresh)
        self.assertEqual(self.store.get_media("video:42_83")["status"], "done")
        self.assertEqual(self.store.task("video.details:42_82", "Видео")["status"], "error")

    def test_failed_documents_get_new_urls_without_refreshing_saved_files(self):
        collector = Collector(FakeAPI(self.store, self.stop), self.store, self.stop)
        source = source_context("dialogs", 50, "Тест")
        collector.attachment("doc", DOCUMENT, source)
        collector.attachment("doc", dict(DOCUMENT, id=92), source)
        self.store.mark_media_error("doc:42_91", "failed", "Истекла ссылка")
        self.store.mark_media_done("doc:42_92", "media/ready.docx", 5, "hash", "application/octet-stream")
        fresh = "https://cdn.example.test/fresh.docx"
        requests = []

        def call(method, **params):
            requests.append((method, params))
            return [dict(DOCUMENT, url=fresh)]

        collector.api.call = call
        with contextlib.redirect_stdout(io.StringIO()):
            collector.refresh_media_urls("doc")
        self.assertEqual(requests, [("docs.getById", {"docs": "42_91"})])
        self.assertEqual(self.store.get_media("doc:42_91")["url"], fresh)
        self.assertEqual(self.store.media_sources("doc:42_91")[0]["category"], "dialogs")

    def test_interruption_checkpoints_only_committed_pages(self):
        api = FakeAPI(self.store, self.stop)
        collector = Collector(api, self.store, self.stop)

        def consume(item):
            collector.ingest("message", item, key=str(item["id"]), source=source_context("dialogs", 50, "Тест"))
            self.stop.set()

        with self.assertRaises(Stopped):
            collector.pages("messages.getHistory", {"peer_id": 50}, "Тестовая история", consume, count=200)
        task = self.store.rows("SELECT * FROM tasks")[0]
        self.assertEqual(task["offset"], 0)
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(self.store.status()["counts"]["message"], 1)

    def test_repeated_page_fails_instead_of_looping(self):
        collector = Collector(FakeAPI(self.store, self.stop), self.store, self.stop)
        collector.api.call = lambda *args, **params: {"count": 10, "items": [{"id": 1}, {"id": 2}]}
        with self.assertRaisesRegex(RuntimeError, "повторяет"):
            collector.pages("docs.get", {}, "Повторяющийся ответ", lambda item: None, count=2)
        self.assertEqual(self.store.rows("SELECT status FROM tasks")[0]["status"], "error")


if __name__ == "__main__":
    unittest.main()
