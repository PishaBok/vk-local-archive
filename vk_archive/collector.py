"""Read-only collection of the VK sections supported by the local archive."""

import hashlib
import json
from typing import Any, Dict, Optional

from .api import ApiError, Stopped
from .media import choose_picture


PROFILE_FIELDS = (
    "photo_100,photo_200_orig,photo_max_orig,domain,city,country,bdate,sex,status,"
    "about,activities,interests,music,movies,tv,books,games,quotes,personal,"
    "home_town,education,career,military,contacts,relation,relatives,occupation,site"
)
ACTOR_FIELDS = {
    "from_id", "owner_id", "user_id", "member_id", "reply_to_user", "signer_id",
    "created_by", "invited_by", "admin_id",
}
FATAL_CODES = {5, 14, 17}


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def object_key(value: Dict[str, Any]) -> str:
    return "%s_%s" % (value.get("owner_id", value.get("from_id", 0)), value.get("id", 0))


def digest(value: Any, length: int = 24) -> str:
    return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()[:length]


def source_context(category: str, ident: Any, name: str, date: Any = 0) -> Dict[str, Any]:
    return {"category": category, "id": str(ident), "name": str(name), "date": int(date or 0)}


class Collector:
    """Collect API objects first; the download command saves their media later.

    Offsets are checkpoints, not an atomic server snapshot. Changed/deleted data
    during collection can affect pagination; raw responses remain available.
    """

    def __init__(self, api, store, stop, refresh=False):
        self.api = api
        self.store = store
        self.stop = stop
        self.uid = 0
        self.actor_ids = set()
        self.refresh = refresh

    def clean_error(self, exc: Exception) -> str:
        return self.api.clean(str(exc))

    def actor_name(self, ident: int) -> str:
        rows = self.store.rows("SELECT name FROM actors WHERE id=?", (ident,))
        if rows and rows[0].get("name"):
            return rows[0]["name"]
        return ("Сообщество " if ident < 0 else "Пользователь ") + str(abs(ident))

    def register_actor(self, value: Dict[str, Any], group: bool = False) -> None:
        if not isinstance(value, dict) or not value.get("id"):
            return
        self.store.actor(value, group=group)
        ident = -abs(int(value["id"])) if group else int(value["id"])
        url = choose_picture(value)
        name = self.actor_name(ident)
        category = "groups" if group else "people"
        if url:
            self.store.asset(
                "avatar:%s_%s" % (ident, hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]), url, "avatar", name=name,
                source=source_context(category, abs(ident), name), metadata=value,
            )

    def asset(self, key: str, url: Optional[str], kind: str, value: Dict[str, Any],
              source: Optional[Dict[str, Any]], name: str = "", reason: str = "") -> None:
        self.store.asset(
            key, url or "", kind, name=name or value.get("title", ""), reason=reason,
            expected_size=value.get("size", 0) if kind == "doc" else 0,
            source=source, metadata=value,
        )

    def attachment(self, kind: str, value: Dict[str, Any], source=None) -> None:
        kind = "voice" if kind == "audio_message" else kind
        key = object_key(value)
        url = ""
        reason = "API не вернул адрес файла"
        name = value.get("title", "")
        if kind == "photo":
            url = choose_picture(value)
            name = value.get("text") or "Фотография"
        elif kind == "voice":
            url = value.get("link_mp3") or value.get("link_ogg") or ""
            name = "Голосовое сообщение"
        elif kind in ("doc", "audio"):
            url = value.get("url") or ""
            if kind == "audio":
                name = " — ".join(str(value.get(k, "")) for k in ("artist", "title")).strip(" —")
        elif kind == "sticker":
            key = str(value.get("sticker_id", value.get("id", 0)))
            url = choose_picture(value)
            name = "Стикер " + key
        elif kind == "video":
            variants = []
            for field, address in (value.get("files") or {}).items():
                if field.startswith("mp4_") and field[4:].isdigit() and isinstance(address, str):
                    variants.append((int(field[4:]), address))
            if variants:
                url = max(variants)[1]
            else:
                reason = "Нет прямого MP4; сохранены метаданные. Поток можно попробовать скачать с --yt-dlp"
            preview = choose_picture({"images": value.get("image", [])})
            if preview:
                self.asset("video_thumb:" + key, preview, "preview", value, source, name="Превью " + (name or key))
        self.asset(kind + ":" + key, url, kind, value, source, name=name, reason=reason)
        if kind in ("photo", "video", "audio", "doc"):
            self.store.put(kind, key, value, parent="attachments")

    def scan(self, value: Any, source=None) -> None:
        """Find attachments recursively without losing reply/forward context."""
        if isinstance(value, list):
            for child in value:
                self.scan(child, source)
            return
        if not isinstance(value, dict):
            return
        for field in ACTOR_FIELDS:
            ident = value.get(field)
            if isinstance(ident, int) and ident and abs(ident) < 2000000000:
                self.actor_ids.add(ident)
        if value.get("first_name") and value.get("id"):
            self.register_actor(value)
        elif value.get("name") and value.get("id") and value.get("type") in ("group", "page", "event"):
            self.register_actor(value, group=True)
        for item in value.get("profiles", []):
            self.register_actor(item)
        for item in value.get("groups", []):
            if isinstance(item, dict):
                self.register_actor(item, group=True)

        kind = value.get("type")
        media_kinds = ("photo", "video", "audio", "doc", "audio_message", "sticker")
        if kind in media_kinds and isinstance(value.get(kind), dict):
            self.attachment(kind, value[kind], source)
        if "album_id" in value and "owner_id" in value and "id" in value:
            self.attachment("photo", value, source)
        elif "owner_id" in value and "id" in value:
            if "ext" in value and "url" in value:
                self.attachment("doc", value, source)
            elif "artist" in value and "duration" in value:
                self.attachment("audio", value, source)
            elif "player" in value or "files" in value or ("duration" in value and "image" in value):
                self.attachment("video", value, source)
        if "link_mp3" in value or "link_ogg" in value:
            self.attachment("audio_message", value, source)
        if kind == "wall" and isinstance(value.get("wall"), dict):
            post = value["wall"]
            self.store.put("post", object_key(post), post, parent="attached")
        if isinstance(value.get("gift"), dict):
            gift = value["gift"]
            url = choose_picture(gift)
            if url:
                self.asset("gift:" + str(gift.get("id", digest(gift))), url, "gift", gift, source, name="Подарок")
        # Covers, chat photos and document/link previews are useful independently.
        preview = choose_picture(value)
        has_object_media = "owner_id" in value and "id" in value and ("album_id" in value or "files" in value or "player" in value)
        is_actor = bool(value.get("first_name") or (value.get("name") and value.get("id") and not value.get("owner_id")))
        is_catalog_image = "sticker_id" in value or "thumb_256" in value
        if preview and not has_object_media and not is_actor and not is_catalog_image:
            chat_avatar = source and source.get("category") == "dialogs" and any(key.startswith("photo_") for key in value)
            self.asset("image:" + hashlib.sha256(preview.encode("utf-8")).hexdigest()[:32], preview,
                       "avatar" if chat_avatar else "preview", value, source, name="Фото беседы" if chat_avatar else "Изображение")
        for child in value.values():
            if isinstance(child, (dict, list)):
                self.scan(child, source)

    def ingest(self, kind: str, value: Dict[str, Any], parent: str = "", key=None, source=None) -> None:
        if not isinstance(value, dict):
            return
        self.store.put(kind, key if key is not None else object_key(value), value, parent=parent)
        self.scan(value, source)

    def pages(self, method: str, params: Dict[str, Any], label: str, consume,
              count: int = 100, task_key: Optional[str] = None) -> None:
        key = task_key or method + ":" + digest(params, 16)
        state = self.store.task(key, label)
        if state["status"] == "done":
            return
        offset = int(state.get("offset") or 0)
        received = int(state.get("received") or 0)
        previous = None
        self.store.task(key, label, status="running", error="")
        try:
            while not self.stop.is_set():
                data = self.api.call(method, **dict(params, offset=offset, count=count))
                if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                    raise RuntimeError(method + ": неожиданный ответ; исходник сохранён в raw")
                items = data["items"]
                self.scan({"profiles": data.get("profiles", []), "groups": data.get("groups", [])})
                fingerprint = digest(items, 64)
                if items and fingerprint == previous:
                    raise RuntimeError("API повторяет одну страницу; обход раздела остановлен")
                previous = fingerprint
                for item in items:
                    if self.stop.is_set():
                        raise Stopped()
                    consume(item)
                offset += len(items)
                received += len(items)
                expected = data.get("count") if isinstance(data.get("count"), int) else None
                self.store.task(key, label, offset=offset, received=received, expected=expected, status="running")
                if not items or (len(items) < count and (expected is None or offset >= expected)):
                    self.store.task(key, label, status="done")
                    return
            raise Stopped()
        except Stopped:
            self.store.task(key, label, status="interrupted")
            raise
        except Exception as exc:
            self.store.task(key, label, status="error", error=self.clean_error(exc))
            raise

    def step(self, label: str, func) -> None:
        if self.stop.is_set():
            raise Stopped()
        self.store.meta("phase", label)
        self.store.log("INFO", label, "Начало")
        try:
            func()
        except ApiError as exc:
            if exc.code in FATAL_CODES:
                raise
            self.store.log("ERROR", label, self.clean_error(exc))
        except Stopped:
            raise
        except Exception as exc:
            self.store.log("ERROR", label, self.clean_error(exc))

    def conversation_source(self, value: Dict[str, Any]) -> Dict[str, Any]:
        conversation = value.get("conversation") or {}
        peer = int((conversation.get("peer") or {}).get("id", 0))
        title = (conversation.get("chat_settings") or {}).get("title") or self.actor_name(peer)
        return source_context("dialogs", peer, title)

    def conversations(self) -> None:
        def save(item):
            peer = (item.get("conversation") or {}).get("peer", {}).get("id")
            if peer is None:
                return
            source = self.conversation_source(item)
            self.ingest("conversation", item, key=str(peer), source=source)
            if abs(peer) < 2000000000 and peer:
                self.actor_ids.add(peer)
        for mode in ("all", "archive"):
            self.step("Диалоги: " + mode, lambda mode=mode: self.pages(
                "messages.getConversations", {"filter": mode, "extended": 1, "fields": PROFILE_FIELDS},
                "Диалоги: " + mode, save, count=200,
            ))
        for row in self.store.rows("SELECT key,data FROM objects WHERE kind='conversation'"):
            peer = int(row["key"])
            value = json.loads(row["data"])
            base_source = self.conversation_source(value)

            def message(item, peer=peer, base_source=base_source):
                ident = item.get("conversation_message_id") or item.get("id") or digest(item, 20)
                source = dict(base_source, date=item.get("date", 0))
                self.ingest("message", item, parent=str(peer), key="%s_%s" % (peer, ident), source=source)
            self.step("История диалога " + str(peer), lambda peer=peer: self.pages(
                "messages.getHistory", {"peer_id": peer, "rev": 1, "extended": 1, "fields": PROFILE_FIELDS},
                "Сообщения " + str(peer), message, count=200,
            ))
            if peer >= 2000000000:
                def member(item, peer=peer, base_source=base_source):
                    self.ingest("member", item, parent=str(peer), key="%s_%s" % (peer, item.get("member_id", 0)), source=base_source)
                self.step("Участники " + str(peer), lambda peer=peer: self.pages(
                    "messages.getConversationMembers", {"peer_id": peer, "extended": 1, "fields": PROFILE_FIELDS},
                    "Участники " + str(peer), member, count=1000,
                ))

    def photos(self) -> None:
        self.step("Список альбомов", lambda: self.pages(
            "photos.getAlbums", {"owner_id": self.uid, "need_system": 1, "need_covers": 1, "photo_sizes": 1},
            "Альбомы", lambda x: self.ingest("album", x, parent="own", source=source_context("albums", x.get("id", 0), x.get("title") or "Альбом")),
        ))
        albums = {}
        for row in self.store.rows("SELECT data FROM objects WHERE kind='album'"):
            item = json.loads(row["data"])
            albums[str(item["id"])] = item.get("title") or "Альбом " + str(item["id"])
        for aid, title in (("profile", "Фотографии профиля"), ("wall", "Фотографии со стены"), ("saved", "Сохранённые фотографии")):
            albums.setdefault(aid, title)
        for aid, title in sorted(albums.items()):
            def save(photo, aid=aid):
                actual_album = str(photo.get("album_id", ""))
                # Prefer a server-returned system album already present in the
                # album list; no hard-coded mapping of VK's negative IDs.
                canonical = actual_album if aid in ("profile", "wall", "saved") and photo.get("owner_id", self.uid) == self.uid and actual_album.startswith("-") and actual_album in albums else aid
                source = source_context("albums", canonical, albums[canonical], photo.get("date", 0))
                self.ingest("photo", photo, parent="album:" + aid, source=source)
                self.store.execute("INSERT OR IGNORE INTO links(kind,key,parent) VALUES ('photo',?,'photos:own')", (object_key(photo),))
                if canonical != aid:
                    self.store.execute("INSERT OR IGNORE INTO links(kind,key,parent) VALUES ('photo',?,?)", (object_key(photo), "album:" + canonical))
            self.step("Фото альбома " + title, lambda aid=aid, title=title: self.pages(
                "photos.get", {"owner_id": self.uid, "album_id": aid, "extended": 1, "photo_sizes": 1},
                "Фото: " + title, save, count=1000,
            ))

    def comments(self) -> None:
        for row in self.store.rows("SELECT key,data FROM objects WHERE kind='post'"):
            post = json.loads(row["data"])
            if not post.get("id") or not post.get("owner_id"):
                continue
            parent = row["key"]
            owner = post["owner_id"]
            links = self.store.rows("SELECT parent FROM links WHERE kind='post' AND key=?", (parent,))
            bookmarked = any(x["parent"] == "bookmarks" for x in links)
            base_source = source_context("bookmarks" if bookmarked else "wall", "posts" if bookmarked else owner,
                                         "Посты в закладках" if bookmarked else "Стена " + self.actor_name(owner))
            roots = []

            def save(comment, parent=parent, owner=owner, base_source=base_source):
                source = dict(base_source, date=comment.get("date", 0))
                self.ingest("comment", comment, parent=parent, key="%s_%s" % (owner, comment.get("id", 0)), source=source)
                thread = comment.get("thread") or {}
                for child in thread.get("items", []):
                    self.ingest("comment", child, parent=parent, key="%s_%s" % (owner, child.get("id", 0)),
                                source=dict(base_source, date=child.get("date", 0)))
                if thread.get("count", 0) > len(thread.get("items", [])):
                    roots.append(comment["id"])
            params = {"owner_id": owner, "post_id": post["id"], "sort": "asc", "extended": 1,
                      "fields": PROFILE_FIELDS, "preview_length": 0, "thread_items_count": 10}
            self.step("Комментарии " + parent, lambda: self.pages("wall.getComments", params, "Комментарии " + parent, save))
            for saved in self.store.rows(
                "SELECT o.data FROM objects o WHERE o.kind='comment' AND EXISTS(SELECT 1 FROM links l WHERE l.kind=o.kind AND l.key=o.key AND l.parent=?)",
                (parent,),
            ):
                item = json.loads(saved["data"])
                thread = item.get("thread") or {}
                if thread.get("count", 0) > len(thread.get("items", [])):
                    roots.append(item["id"])
            for root in sorted(set(roots)):
                args = dict(params, comment_id=root)
                self.step("Ветка комментариев %s/%s" % (parent, root), lambda args=args, root=root: self.pages(
                    "wall.getComments", args, "Ответы %s/%s" % (parent, root), save,
                ))

    def videos(self) -> None:
        own_source = source_context("profile", self.uid, self.actor_name(self.uid))
        self.step("Список видео", lambda: self.pages(
            "video.get", {"owner_id": self.uid, "extended": 1}, "Видео",
            lambda x: self.ingest("video", x, parent="own", source=dict(own_source, date=x.get("date", 0))), count=200,
        ))
        for row in self.store.rows("SELECT key,data FROM objects WHERE kind='video'"):
            video = json.loads(row["data"])
            if video.get("files"):
                continue
            ref = object_key(video) + ("_" + video["access_key"] if video.get("access_key") else "")

            def fetch(ref=ref, old=video, key=row["key"]):
                task = "video.details:" + key
                if self.store.task(task, "Файл видео " + key)["status"] == "done":
                    return
                try:
                    result = self.api.call("video.get", videos=ref, extended=1)
                    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                        raise RuntimeError("video.get: неожиданный ответ")
                    self.scan({"profiles": result.get("profiles", []), "groups": result.get("groups", [])})
                    for item in result["items"]:
                        self.ingest("video", dict(old, **item))
                    self.store.task(task, "Файл видео " + key, status="done")
                except Exception as exc:
                    self.store.task(task, "Файл видео " + key, status="error", error=self.clean_error(exc))
                    raise
            self.step("Файл видео " + row["key"], fetch)

    def resolve_actors(self) -> None:
        # Finished pages are skipped on resume, so recover IDs from stored JSON.
        def collect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in ACTOR_FIELDS and isinstance(child, int) and child and abs(child) < 2000000000:
                        self.actor_ids.add(child)
                    if key == "peer" and isinstance(child, dict):
                        ident = child.get("id")
                        if isinstance(ident, int) and ident and abs(ident) < 2000000000:
                            self.actor_ids.add(ident)
                    if isinstance(child, (dict, list)):
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        for row in self.store.db().execute("SELECT data FROM objects"):
            collect(json.loads(row[0]))
        existing = {row["id"] for row in self.store.rows("SELECT id FROM actors")}
        missing = sorted(self.actor_ids - existing)
        for group in (False, True):
            wanted = [abs(x) for x in missing if (x < 0) == group]
            for start in range(0, len(wanted), 100):
                batch = ",".join(map(str, wanted[start:start + 100]))

                def fetch(batch=batch, group=group):
                    if group:
                        result = self.api.call("groups.getById", group_ids=batch, fields="description,photo_200")
                        items = result.get("groups", []) if isinstance(result, dict) else result
                    else:
                        items = self.api.call("users.get", user_ids=batch, fields=PROFILE_FIELDS)
                    if not isinstance(items, list):
                        raise RuntimeError("Неожиданный ответ при получении имён")
                    for item in items:
                        self.register_actor(item, group=group)
                self.step("Имена и аватары", fetch)
        for row in self.store.rows("SELECT id,name FROM actors"):
            self.store.rename_source("groups" if row["id"] < 0 else "people", abs(row["id"]), row["name"])
        for row in self.store.rows("SELECT key,data FROM objects WHERE kind='conversation'"):
            source = self.conversation_source(json.loads(row["data"]))
            self.store.rename_source("dialogs", row["key"], source["name"])

    def run(self) -> bool:
        try:
            profile = self.api.call("users.get", _archive=False, fields=PROFILE_FIELDS)
            if not isinstance(profile, list) or not profile:
                raise RuntimeError("users.get не вернул владельца токена")
            self.uid = int(profile[0]["id"])
            owner = self.store.meta("owner_id")
            if owner and int(owner) != self.uid:
                raise RuntimeError("В этой папке архив другого аккаунта. Укажите другую папку через --out")
            if self.refresh:
                self.store.reset_tasks()
            self.api.save_raw("users.get", {"fields": PROFILE_FIELDS}, profile)
            self.store.meta("owner_id", self.uid)
            self.store.meta("api_version", self.api.version)
            self.store.meta("run_state", "running")
            self.register_actor(profile[0])
            own_source = source_context("profile", self.uid, self.actor_name(self.uid))
            self.ingest("profile", profile[0], key=str(self.uid), source=own_source)
            self.step("Дополнительные данные профиля", lambda: self.store.meta("profile_info", self.api.call("account.getProfileInfo")))
            self.step("Права токена", lambda: self.store.meta("permissions", self.api.call("account.getAppPermissions")))
            self.step("Переписка", self.conversations)
            self.step("Фотографии", self.photos)
            self.step("Документы", lambda: self.pages(
                "docs.get", {"owner_id": self.uid}, "Документы",
                lambda x: self.ingest("doc", x, parent="own", source=dict(own_source, date=x.get("date", 0))),
            ))
            self.step("Стена", lambda: self.pages(
                "wall.get", {"owner_id": self.uid, "filter": "all", "extended": 1, "fields": PROFILE_FIELDS}, "Стена",
                lambda x: self.ingest("post", x, parent="wall:" + str(self.uid), source=source_context("wall", self.uid, "Моя стена", x.get("date", 0))),
            ))

            def friend(item):
                self.ingest("friend", item, key=str(item.get("id", 0)))
                self.register_actor(item)
            self.step("Друзья", lambda: self.pages("friends.get", {"user_id": self.uid, "fields": PROFILE_FIELDS}, "Друзья", friend, count=1000))

            def group(item):
                self.ingest("group", item, key=str(item.get("id", 0)))
                self.register_actor(item, group=True)
            self.step("Сообщества", lambda: self.pages(
                "groups.get", {"user_id": self.uid, "extended": 1, "fields": "description,members_count,photo_200"}, "Сообщества", group, count=1000,
            ))
            self.step("Подарки", lambda: self.pages(
                "gifts.get", {"user_id": self.uid}, "Подарки",
                lambda x: self.ingest("gift", x, key=str(x.get("id", 0)), source=source_context("gifts", self.uid, "Мои подарки", x.get("date", 0))),
            ))

            def bookmark(item):
                kind = item.get("type", "unknown")
                body = item.get(kind) or {}
                ident = kind + ":" + object_key(body) if body.get("id") else digest(item)
                source = source_context("bookmarks", "content", "Закладки", item.get("added_date", body.get("date", 0)))
                self.ingest("bookmark", item, key=ident, source=source)
                if kind == "post":
                    self.ingest("post", body, parent="bookmarks", source=source)
            self.step("Закладки", lambda: self.pages("fave.get", {"extended": 1, "fields": PROFILE_FIELDS}, "Закладки", bookmark))

            def page(item):
                body = item.get("user") or item.get("group") or {}
                ident = "page:%s:%s" % (item.get("type", "unknown"), body.get("id", digest(item, 16)))
                self.ingest("bookmark", item, key=ident, source=source_context("bookmarks", "pages", "Страницы в закладках"))
                if body.get("id"):
                    self.register_actor(body, group="group" in item)
            self.step("Страницы в закладках", lambda: self.pages("fave.getPages", {}, "Страницы в закладках", page))

            def subscription(item):
                group = item.get("type") in ("group", "page", "event") or (item.get("name") and not item.get("first_name"))
                self.ingest("subscription", item, key=("group:" if group else "user:") + str(item.get("id", 0)))
                self.register_actor(item, group=bool(group))
            self.step("Подписки", lambda: self.pages(
                "users.getSubscriptions", {"user_id": self.uid, "extended": 1, "fields": PROFILE_FIELDS}, "Подписки", subscription, count=200,
            ))
            self.step("Подписчики", lambda: self.pages(
                "users.getFollowers", {"user_id": self.uid, "fields": PROFILE_FIELDS}, "Подписчики",
                lambda x: self.ingest("follower", x, key=str(x.get("id", 0))), count=1000,
            ))
            self.store.task("audio.library", "Музыкальная коллекция", status="unsupported", error=(
                "Сохраняются аудиовложения и возвращённые API адреса файлов. Отдельная музыкальная коллекция не выгружается: доступный публичный метод не подтверждён."
            ))
            self.step("Комментарии", self.comments)
            self.step("Видеозаписи", self.videos)
            self.step("Имена участников", self.resolve_actors)
            self.store.meta("run_state", "api_finished")
            self.store.meta("phase", "Сбор данных API завершён")
            return True
        except Stopped:
            self.store.meta("run_state", "interrupted")
            self.store.meta("phase", "Сбор данных прерван")
            return False
        except Exception as exc:
            self.store.meta("run_state", "error")
            self.store.meta("phase", "Сбор данных остановлен с ошибкой")
            self.store.log("ERROR", "Выгрузка остановлена", self.clean_error(exc))
            return False
