"""Portable SQLite archive. Paths in the database always use forward slashes."""

import datetime as dt
import json
import sqlite3
import threading
import unicodedata
import urllib.parse
from pathlib import Path

SCHEMA_VERSION = 2
_UNSET = object()
ACTOR_FIELDS = {"from_id", "owner_id", "user_id", "member_id", "reply_to_user", "signer_id", "created_by", "invited_by", "admin_id"}
KINDS = {"profile", "conversation", "message", "member", "album", "photo", "video", "audio", "doc", "post", "comment", "friend", "group", "gift", "bookmark", "follower", "subscription"}
LINK_FILTER = "EXISTS(SELECT 1 FROM links l WHERE l.kind=o.kind AND l.key=o.key AND l.parent=?)"
SOURCE_FILTER = "('video:' || o.key) IN (SELECT s.asset_key FROM asset_sources s WHERE s.category=?)"


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def search_text(value):
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def message_contains(raw, query):
    def matches(message):
        if not isinstance(message, dict):
            return False
        if query in search_text(str(message.get("text") or message.get("body") or "")):
            return True
        return matches(message.get("reply_message")) or any(matches(item) for item in message.get("fwd_messages") or [])

    return matches(json.loads(raw))


def photo_in_album(raw, album_id, owner_id):
    photo = json.loads(raw)
    return str(photo.get("album_id")) == album_id and str(photo.get("owner_id")) == owner_id


def _picture(obj):
    sizes = [item for key in ("sizes", "images", "images_with_background") for item in obj.get(key, []) if isinstance(item, dict) and (item.get("url") or item.get("src"))]
    if sizes:
        item = max(sizes, key=lambda x: int(x.get("width", 0) or 0) * int(x.get("height", 0) or 0))
        return item.get("url") or item.get("src")
    for key in ("photo_max_orig", "photo_200_orig", "photo_200", "photo_100", "photo_50"):
        if isinstance(obj.get(key), str) and obj[key].startswith("https://"):
            return obj[key]
    return ""


class Store:
    def __init__(self, root, create=True):
        self.root = Path(root).expanduser().resolve()
        self.local = threading.local()
        self.readonly = not create
        path = self.root / "archive.sqlite3"
        if not create:
            if not path.is_file():
                raise RuntimeError("Архив не найден: %s. Сначала запустите download.py или укажите --out." % path)
            try:
                self._validate_schema()
            except Exception:
                self.close()
                raise
            return
        self.root.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            tables = {r[0] for r in self.db().execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                try:
                    self._validate_schema()
                except Exception:
                    self.close()
                    raise
        self.db().executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS objects(kind TEXT,key TEXT,title TEXT,ts INTEGER,data TEXT,PRIMARY KEY(kind,key));
        CREATE INDEX IF NOT EXISTS objects_order ON objects(kind,ts,key);
        CREATE TABLE IF NOT EXISTS links(kind TEXT,key TEXT,parent TEXT,PRIMARY KEY(kind,key,parent));
        CREATE INDEX IF NOT EXISTS links_parent ON links(kind,parent,key);
        CREATE TABLE IF NOT EXISTS actors(id INTEGER PRIMARY KEY,name TEXT,avatar TEXT,data TEXT);
        CREATE TABLE IF NOT EXISTS tasks(key TEXT PRIMARY KEY,label TEXT,offset INTEGER DEFAULT 0,expected INTEGER,received INTEGER DEFAULT 0,status TEXT,error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts TEXT,level TEXT,section TEXT,message TEXT);
        CREATE TABLE IF NOT EXISTS assets(key TEXT PRIMARY KEY,url TEXT,kind TEXT,name TEXT,ts INTEGER DEFAULT 0,status TEXT,path TEXT DEFAULT '',size INTEGER DEFAULT 0,sha256 TEXT DEFAULT '',mime TEXT DEFAULT '',error TEXT DEFAULT '',expected_size INTEGER DEFAULT 0,ext TEXT DEFAULT '',metadata TEXT DEFAULT '{}');
        CREATE INDEX IF NOT EXISTS assets_url ON assets(url);
        CREATE TABLE IF NOT EXISTS asset_sources(asset_key TEXT,category TEXT,source_id TEXT,name TEXT,ts INTEGER DEFAULT 0,path TEXT DEFAULT '',PRIMARY KEY(asset_key,category,source_id));
        CREATE INDEX IF NOT EXISTS sources_asset ON asset_sources(asset_key);
        """)
        self.db().execute("PRAGMA user_version=%s" % SCHEMA_VERSION)
        self.db().commit()
        self.meta("schema_version", SCHEMA_VERSION)

    def _validate_schema(self):
        version = self.db().execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise RuntimeError("Неподдерживаемая версия архива: %s (нужна %s). Укажите новую папку --out; старый формат не изменяется." % (version, SCHEMA_VERSION))

    def db(self):
        if not hasattr(self.local, "connection"):
            path = self.root / "archive.sqlite3"
            if self.readonly:
                uri = path.as_uri() + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=30)
                conn.execute("PRAGMA query_only=ON")
            else:
                conn = sqlite3.connect(str(path), timeout=30)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            conn.create_function("CASEFOLD", 1, lambda value: str(value or "").casefold())
            conn.create_function("MESSAGE_CONTAINS", 2, message_contains)
            conn.create_function("PHOTO_IN_ALBUM", 3, photo_in_album)
            self.local.connection = conn
        return self.local.connection

    def close(self):
        if hasattr(self.local, "connection"):
            self.local.connection.close()
            del self.local.connection

    def execute(self, sql, args=()):
        cur = self.db().execute(sql, args)
        self.db().commit()
        return cur

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db().execute(sql, args)]

    def meta(self, key, value=_UNSET):
        if value is not _UNSET:
            self.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, dumps(value)))
            return value
        row = self.db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def log(self, level, section, message):
        self.execute("INSERT INTO events(ts,level,section,message) VALUES (?,?,?,?)", (now(), level, section, str(message)))
        print("[%s] %s: %s" % (level, section, message), flush=True)

    def put(self, kind, key, data, parent="", title=""):
        if kind not in KINDS:
            raise ValueError("Неизвестный тип объекта: " + str(kind))
        if kind in {"photo", "video", "audio", "doc"}:
            old = self.db().execute("SELECT data FROM objects WHERE kind=? AND key=?", (kind, str(key))).fetchone()
            if old:
                data = dict(json.loads(old[0]), **data)
        title = title or data.get("title") or data.get("name") or str(data.get("text", ""))[:180] or str(key)
        timestamp = (data.get("last_message") or {}).get("date", 0) if kind == "conversation" else data.get("date", 0)
        self.execute("""INSERT INTO objects VALUES (?,?,?,?,?) ON CONFLICT(kind,key)
            DO UPDATE SET title=excluded.title,ts=excluded.ts,data=excluded.data""", (kind, str(key), str(title), int(timestamp or 0), dumps(data)))
        if parent:
            self.execute("INSERT OR IGNORE INTO links VALUES (?,?,?)", (kind, str(key), str(parent)))

    def actor(self, data, group=False):
        if not isinstance(data, dict) or not data.get("id"):
            return None
        ident = -abs(int(data["id"])) if group else int(data["id"])
        old = self.db().execute("SELECT data FROM actors WHERE id=?", (ident,)).fetchone()
        merged = json.loads(old[0]) if old else {}
        merged.update({key: value for key, value in data.items() if value is not None and value != ""})
        name = merged.get("name") if group else " ".join(str(merged.get(key, "")) for key in ("first_name", "last_name")).strip()
        self.execute("INSERT OR REPLACE INTO actors VALUES (?,?,?,?)", (ident, name or "Имя не получено", _picture(merged), dumps(merged)))
        return ident

    def task(self, key, label, **fields):
        self.execute("INSERT OR IGNORE INTO tasks(key,label,status) VALUES (?,?,'pending')", (key, label))
        names = [key for key in fields if key in {"offset", "expected", "received", "status", "error"}]
        if names:
            self.execute("UPDATE tasks SET " + ",".join(key + "=?" for key in names) + " WHERE key=?", tuple(fields[key] for key in names) + (key,))
        return dict(self.db().execute("SELECT * FROM tasks WHERE key=?", (key,)).fetchone())

    def reset_tasks(self):
        self.execute("UPDATE tasks SET offset=0,received=0,status='pending',error='' ")

    def asset(self, key, url, kind, name="", reason="", expected_size=0, source=None, metadata=None):
        old = self.get_media(key)
        extra = json.loads(old["metadata"]) if old else {}
        extra.update(metadata or {})
        if old is None:
            self.execute("INSERT INTO assets(key,url,kind,name,ts,status,error,expected_size,ext,metadata) VALUES (?,?,?,?,?,?,?,?,?,?)", (
                key, url or "", kind, name or extra.get("title", ""), int(extra.get("date", 0) or 0),
                "pending" if url else "unavailable", "" if url else reason, int(expected_size or 0), extra.get("ext", ""), dumps(extra),
            ))
        else:
            state, error = old["status"], old["error"]
            if url and (url != old["url"] or state == "unavailable") and state not in {"done", "partial"}:
                state, error = "pending", ""
            self.execute("UPDATE assets SET url=?,name=?,metadata=?,expected_size=?,ext=?,status=?,error=?,ts=? WHERE key=?", (
                url or old["url"], name or old["name"], dumps(extra), int(expected_size or old["expected_size"] or 0),
                extra.get("ext") or old["ext"], state, error,
                int(old["ts"] or extra.get("date", 0) or 0), key,
            ))
        if source is not None or not self.media_sources(key):
            source = source or {"category": "unknown", "id": "", "name": "Прочее", "date": 0}
            category = str(source.get("category") or "unknown")
            source_id = str(source.get("id", source.get("identifier", "")))
            title = str(source.get("name", source.get("title", "")) or "")
            timestamp = int(source.get("date", 0) or 0)
            self.execute("""INSERT INTO asset_sources(asset_key,category,source_id,name,ts) VALUES (?,?,?,?,?)
                ON CONFLICT(asset_key,category,source_id) DO UPDATE SET
                name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE asset_sources.name END,
                ts=CASE WHEN asset_sources.ts=0 THEN excluded.ts WHEN excluded.ts=0 THEN asset_sources.ts ELSE MIN(asset_sources.ts,excluded.ts) END""", (key, category, source_id, title, timestamp))

    def get_media(self, key):
        row = self.db().execute("SELECT * FROM assets WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def media_sources(self, key):
        return self.rows("SELECT * FROM asset_sources WHERE asset_key=? ORDER BY category,source_id", (key,))

    def media_path(self, row, sources=None):
        if row["status"] not in {"done", "partial"}:
            return None
        media = (self.root / "media").resolve()
        if self.root not in media.parents:
            return None
        if sources is None:
            sources = self.media_sources(row["key"])
        for relative in [row["path"], *(source["path"] for source in sources)]:
            if relative:
                try:
                    path = (self.root / relative).resolve()
                    if media in path.parents and path.is_file():
                        return path
                except (OSError, RuntimeError, ValueError):
                    continue
        return None

    def rename_source(self, category, source_id, name):
        self.execute("UPDATE asset_sources SET name=? WHERE category=? AND source_id=?", (name, category, str(source_id)))

    def mark_media_done(self, key, path, size, sha256, mime):
        path = Path(path)
        if path.is_absolute():
            path = path.resolve().relative_to(self.root)
        self.execute("UPDATE assets SET status='done',path=?,size=?,sha256=?,mime=?,error='' WHERE key=?", (path.as_posix(), size, sha256, mime, key))

    def mark_media_error(self, key, status, error):
        self.execute("UPDATE assets SET status=?,error=? WHERE key=?", (status, str(error), key))

    def set_source_path(self, asset_key, category, source_id, path):
        path = Path(path)
        if path.is_absolute():
            path = path.resolve().relative_to(self.root)
        self.execute("UPDATE asset_sources SET path=? WHERE asset_key=? AND category=? AND source_id=?", (path.as_posix(), asset_key, category, str(source_id)))

    def status(self, brief=False):
        result = {
            "meta": {row["key"]: json.loads(row["value"]) for row in self.rows("SELECT * FROM meta")},
            "counts": {row["kind"]: row["n"] for row in self.rows("SELECT kind,COUNT(*) n FROM objects GROUP BY kind")},
            "assets": {row["status"]: row["n"] for row in self.rows("SELECT status,COUNT(*) n FROM assets GROUP BY status")},
            "actor_count": self.db().execute("SELECT COUNT(*) FROM actors").fetchone()[0],
        }
        result["video_counts"] = {
            name: self.db().execute("SELECT COUNT(*) FROM objects o WHERE o.kind='video' AND " + clause, (value,)).fetchone()[0]
            for name, clause, value in (("video", LINK_FILTER, "own"), ("dialog_video", SOURCE_FILTER, "dialogs"))
        }
        if brief:
            result["meta"] = {key: value for key, value in result["meta"].items() if key in {"owner_id", "phase", "run_state"}}
        else:
            result.update({
                "tasks": self.rows("SELECT * FROM tasks ORDER BY label"),
                "events": self.rows("SELECT * FROM events ORDER BY id DESC LIMIT 150"),
                "missing": self.rows("SELECT key,kind,name,status,error FROM assets WHERE status NOT IN ('done') LIMIT 500"),
            })
        return result

    def report(self):
        result = self.status()
        result["missing"] = self.rows("SELECT key,kind,name,status,error FROM assets WHERE status<>'done'")
        result["created_at"] = now()
        return result

    def bundle(self, items):
        urls, ids, refs = set(), set(), set()

        def walk(value):
            if isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                if isinstance(value.get("peer"), dict):
                    ident = value["peer"].get("id")
                    if isinstance(ident, int) and ident and abs(ident) < 2000000000:
                        ids.add(ident)
                if value.get("first_name") and isinstance(value.get("id"), int):
                    ids.add(value["id"])
                if value.get("kind") in {"group", "friend", "follower", "subscription"} and isinstance(value.get("data"), dict):
                    data = value["data"]
                    if data.get("id"):
                        group = value["kind"] == "group" or (value["kind"] == "subscription" and not data.get("first_name"))
                        ids.add(-abs(int(data["id"])) if group else int(data["id"]))
                if isinstance(value.get("group"), dict) and value["group"].get("id"):
                    ids.add(-abs(int(value["group"]["id"])))
                if "owner_id" in value and "id" in value:
                    ident = "%s_%s" % (value["owner_id"], value["id"])
                    refs.update(kind + ":" + ident for kind in ("photo", "video", "audio", "doc", "voice", "preview", "video_thumb"))
                if value.get("sticker_id"):
                    refs.add("sticker:" + str(value["sticker_id"]))
                for key, item in value.items():
                    if key in ACTOR_FIELDS or key == "peer_id":
                        if isinstance(item, int) and item and abs(item) < 2000000000:
                            ids.add(item)
                    walk(item)
            elif isinstance(value, str) and value.startswith(("https://", "http://")):
                urls.add(value)

        walk(items)
        owner = self.meta("owner_id")
        if owner:
            ids.add(owner)
        actors, assets = {}, {}
        for ident in ids:
            row = self.db().execute("SELECT * FROM actors WHERE id=?", (ident,)).fetchone()
            if row:
                actor = dict(row)
                actor["data"] = json.loads(actor["data"])
                actors[str(ident)] = actor
                if actor["avatar"]:
                    urls.add(actor["avatar"])
        for url in urls:
            for row in self.db().execute("SELECT * FROM assets WHERE url=?", (url,)):
                assets[row["key"]] = dict(row)
        for ref in refs:
            row = self.get_media(ref)
            if row:
                assets[ref] = row
        for asset in assets.values():
            asset.pop("metadata", None)
            asset["sources"] = self.media_sources(asset["key"])
            path = self.media_path(asset, asset["sources"])
            asset["local"] = "/media/" + urllib.parse.quote(asset["key"], safe="") if path else ""
            if path:
                asset["path"] = path.relative_to(self.root).as_posix()
        return {"items": items, "actors": actors, "assets": list(assets.values())}

    def objects(self, kind, parent="", offset=0, limit=100, query="", order="desc", after=None, before=None, source=""):
        if kind not in KINDS:
            raise ValueError("Неизвестный раздел")
        clauses, params = ["o.kind=?"], [kind]
        table = "objects o"
        if parent:
            album_id = str(parent).removeprefix("album:")
            owner_id = self.meta("owner_id") if kind == "photo" and str(parent).startswith("album:") else None
            if owner_id and album_id.lstrip("-").isdigit():
                # Older archives can have the photo from a dialog but no album link.
                clauses.append("(" + LINK_FILTER + " OR PHOTO_IN_ALBUM(o.data,?,?))")
                params.extend((str(parent), album_id, str(owner_id)))
            else:
                table = "links l JOIN objects o ON o.kind=l.kind AND o.key=l.key"
                clauses = ["l.kind=?", "l.parent=?"]
                params.append(str(parent))
        if source:
            if kind != "video" or source != "dialogs":
                raise ValueError("Неизвестный источник видео")
            clauses.append(SOURCE_FILTER)
            params.append(source)
        if kind == "message" and search_text(query):
            clauses.append("MESSAGE_CONTAINS(o.data,?)")
            params.append(search_text(query))
        elif query.strip():
            clauses.append("(CASEFOLD(o.data) LIKE ? ESCAPE '\\' OR CASEFOLD(o.title) LIKE ? ESCAPE '\\' OR EXISTS(SELECT 1 FROM actors a WHERE CAST(a.id AS TEXT)=o.key AND CASEFOLD(a.name) LIKE ? ESCAPE '\\'))")
            needle = query.strip().casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend(("%" + needle + "%",) * 3)
        for value, condition in ((after, "o.ts>=?"), (before, "o.ts<?")):
            if value is not None:
                clauses.append(condition)
                params.append(int(value))
        where = " AND ".join(clauses)
        base = " FROM " + table + " WHERE " + where
        total = self.db().execute("SELECT COUNT(*)" + base, params).fetchone()[0]
        direction = "ASC" if order.lower() == "asc" else "DESC"
        rows = self.rows("SELECT o.*" + base + " ORDER BY o.ts " + direction + ",o.rowid " + direction + " LIMIT ? OFFSET ?", tuple(params) + (min(200, max(1, int(limit))), max(0, int(offset))))
        for row in rows:
            row["data"] = json.loads(row["data"])
        result = self.bundle(rows)
        result["total"] = total
        return result
