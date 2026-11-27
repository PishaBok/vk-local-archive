"""Media downloads and a readable, cross-platform archive layout.

This module never receives the API token. Source URLs come from collected API
objects; the downloader sends no cookies or authentication headers to them.
"""

import contextlib
import datetime as dt
import errno
import hashlib
import http.client
import importlib.util
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union


KIND_FOLDERS = {
    "photo": "photos", "video": "videos", "audio": "audio",
    "voice": "voice", "doc": "documents", "avatar": "avatars",
    "sticker": "stickers", "gift": "gifts", "preview": "previews",
    "image": "previews",
}
SOURCE_PRIORITY = {
    "albums": 0, "profile": 1, "dialogs": 2, "wall": 3,
    "bookmarks": 4, "people": 5, "groups": 6, "gifts": 7, "unknown": 8,
}
SOURCE_ALIASES = {
    "album": "albums", "dialog": "dialogs", "messages": "dialogs",
    "user": "people", "users": "people", "group": "groups",
    "bookmark": "bookmarks", "gift": "gifts",
}
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"}
WINDOWS_RESERVED.update("COM" + digit for digit in "123456789¹²³")
WINDOWS_RESERVED.update("LPT" + digit for digit in "123456789¹²³")
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/avif": ".avif", "image/heic": ".heic",
    "image/heif": ".heif", "image/bmp": ".bmp", "image/tiff": ".tiff",
    "image/svg+xml": ".svg", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/ogg": ".ogg", "application/ogg": ".ogg", "audio/flac": ".flac",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mp4": ".m4a",
    "audio/webm": ".webm", "video/mp4": ".mp4", "video/webm": ".webm",
    "video/quicktime": ".mov", "video/x-matroska": ".mkv",
    "video/mp2t": ".ts", "application/pdf": ".pdf",
}
# OpenSSL X509 verification codes and Windows certificate policy HRESULTs.
CERTIFICATE_ERRORS = {
    9: "сертификат ещё не действует; проверьте дату и время системы",
    10: "срок действия сертификата истёк; проверьте дату и время системы",
    18: "сервер предъявил недоверенный самоподписанный сертификат",
    20: "не удалось построить доверенную цепочку сертификатов",
    21: "не удалось построить доверенную цепочку сертификатов",
    62: "имя сервера не совпадает с сертификатом",
    64: "IP-адрес сервера не совпадает с сертификатом",
    0x800B0101: "сертификат вне срока действия; проверьте дату и время системы",
    0x800B0109: "Windows не доверяет корневому сертификату",
    0x800B010A: "Windows не удалось построить цепочку сертификатов",
    0x800B010F: "имя сервера не совпадает с сертификатом",
}


def _trim_units(value: str, units: int, byte_limit: int = 220) -> str:
    while value and (len(value.encode("utf-16-le")) // 2 > units or len(value.encode("utf-8")) > byte_limit):
        value = value[:-1]
    return value.rstrip(" .")


def safe_component(value: Any, default: str = "unnamed", limit: int = 48) -> str:
    """Keep readable Unicode while respecting Windows filename conventions."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = "".join("_" if char in '<>:"/\\|?*' else char for char in text
                   if unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text or text in {".", ".."}:
        text = default
    if text.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        text = "_" + text
    trimmed = _trim_units(text, max(8, limit))
    if trimmed != text:
        suffix = "~" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:7]
        trimmed = _trim_units(text, max(1, limit - len(suffix))) + suffix
    return trimmed or default


def _extension(value: Any) -> str:
    text = str(value or "").lower().lstrip(".")
    return "." + text if re.fullmatch(r"[a-z0-9]{1,10}", text) else ""


def choose_extension(kind: str, name: str = "", ext: str = "", mime: str = "", url: str = "") -> str:
    """Documents keep their original extension; media use the actual MIME."""
    mime = (mime or "").split(";", 1)[0].lower().strip()
    original = _extension(ext) or _extension(Path(name).suffix)
    if kind == "doc" and original:
        return original
    if mime in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[mime]
    if original:
        return original
    remote = _extension(Path(urllib.parse.urlsplit(url).path).suffix)
    if remote and remote not in {".php", ".html", ".htm", ".asp", ".aspx"}:
        return remote
    guessed = mimetypes.guess_extension(mime) if mime and mime != "application/octet-stream" else None
    if guessed and _extension(guessed):
        return guessed
    if kind in {"photo", "avatar", "sticker", "gift", "preview", "image"}:
        return ".jpg"
    if kind in {"audio", "voice"}:
        return ".mp3"
    return ".mp4" if kind == "video" else ".bin"


def choose_picture(obj: Any) -> str:
    """Find the largest returned image, including old VK photo fields."""
    if not isinstance(obj, dict):
        return ""
    variants = []
    for field in ("sizes", "images", "images_with_background", "image"):
        values = obj.get(field)
        if not isinstance(values, list):
            continue
        variants.extend(x for x in values if isinstance(x, dict) and (x.get("url") or x.get("src")))
    if variants:
        def area(item):
            try:
                return int(item.get("width", 0) or 0) * int(item.get("height", 0) or 0)
            except (ValueError, TypeError):
                return 0
        best = max(variants, key=area)
        return best.get("url") or best.get("src") or ""
    for field in ("photo_max_orig", "photo_2560", "photo_1280", "photo_807", "photo_604", "photo_200_orig", "photo_200", "photo_100", "photo_50", "photo_max", "thumb_512", "thumb_256", "thumb_96", "thumb_48", "cover_src_big", "cover_src"):
        value = obj.get(field)
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return value
    return ""


picture = choose_picture


@dataclass(frozen=True)
class MediaSource:
    category: str = "unknown"
    identifier: str = ""
    title: str = ""
    timestamp: int = 0

    @classmethod
    def from_value(cls, source: Any):
        if isinstance(source, cls):
            return source
        if isinstance(source, Mapping):
            category = str(source.get("category", "unknown"))
            try:
                timestamp = int(source.get("ts", source.get("date", 0)) or 0)
            except (TypeError, ValueError):
                timestamp = 0
            return cls(SOURCE_ALIASES.get(category, category), str(source.get("source_id", source.get("identifier", source.get("id", "")))), str(source.get("name", source.get("title", "")) or ""), timestamp)
        return cls()


def _source_component(source: MediaSource, compact: bool = False) -> str:
    identifier = safe_component(source.identifier or "unknown", limit=22)
    limit = 38 if compact else 50
    title_limit = limit - len(identifier.encode("utf-16-le")) // 2 - 2
    title = safe_component(source.title or source.category, limit=max(8, title_limit))
    return title + "__" + identifier


def plan_path(kind: str, source: Union[MediaSource, Mapping[str, Any]], *, key: str, title: str = "", timestamp: int = 0, ext: str = "", mime: str = "", url: str = "", compact: bool = False) -> Path:
    """Return a relative path: media/type/source/readable-source/date__name__vk_ID.ext."""
    source = MediaSource.from_value(source)
    category = source.category if source.category in SOURCE_PRIORITY else "unknown"
    suffix = choose_extension(kind, title, ext, mime, url)
    original_suffix = Path(title).suffix
    name = title[:-len(original_suffix)] if original_suffix and original_suffix.lower() == suffix.lower() else title
    name = safe_component(name or KIND_FOLDERS.get(kind, kind), limit=24 if compact else 40)
    ref = str(key).split(":", 1)[-1]
    ref = safe_component(ref, limit=24)
    ts = timestamp or source.timestamp
    try:
        stamp = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%d_%H-%M-%SZ") if ts else "undated"
    except (ValueError, TypeError, OSError, OverflowError):
        stamp = "undated"
    filename = "%s__%s__vk_%s%s" % (stamp, name, ref, suffix)
    return Path("media") / KIND_FOLDERS.get(kind, "other") / category / _source_component(source, compact) / filename


class DownloadStopped(Exception):
    """The user stopped the downloader."""


class MediaDownloadError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size: int
    sha256: str
    mime: str


def _http_error(status: int) -> MediaDownloadError:
    message = "Сервер файла вернул HTTP %s" % status
    if status in {401, 403, 404, 410}:
        message += " (файл недоступен или ссылка устарела; попробуйте загрузчик с --refresh)"
    elif status == 407:
        message += " (прокси требует авторизацию)"
    return MediaDownloadError(message, retryable=status in {408, 429, 500, 502, 503, 504})


def _network_error(error: Exception) -> MediaDownloadError:
    """Classify urllib failures without exposing URLs, credentials or raw errors."""
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        message = "Не удалось проверить сертификат HTTPS (TLS)"
        code = getattr(reason, "verify_code", None)
        if isinstance(code, int):
            shown_code = f"0x{code:08X}" if code >= 0x80000000 else str(code)
            message += f", код {shown_code}"
        detail = CERTIFICATE_ERRORS.get(code, "проверьте дату системы и доверенные сертификаты")
        return MediaDownloadError(f"{message}: {detail}")
    if isinstance(reason, ssl.SSLEOFError):
        return MediaDownloadError("Сервер закрыл TLS-соединение до завершения загрузки", retryable=True)
    if isinstance(reason, ssl.SSLError):
        return MediaDownloadError("Ошибка TLS при подключении к серверу файла")
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return MediaDownloadError("Истекло время ожидания ответа сервера файла", retryable=True)
    if isinstance(reason, socket.gaierror):
        return MediaDownloadError("Не удалось определить адрес сервера файла (DNS)", retryable=True)
    if isinstance(reason, http.client.IncompleteRead):
        return MediaDownloadError("Сервер закрыл соединение до получения полного файла", retryable=True)
    # urllib reports a failed HTTPS CONNECT as URLError(OSError(...)).
    proxy = re.match(r"Tunnel connection failed:\s*([1-5]\d{2})\b", str(reason))
    if proxy:
        status = int(proxy.group(1))
        return MediaDownloadError("Прокси отклонил HTTPS-соединение (HTTP %s)" % status, retryable=status in {408, 429, 502, 503, 504})
    code = getattr(reason, "winerror", None) or getattr(reason, "errno", None)
    if code in {errno.EACCES, errno.EPERM, 10013}:
        return MediaDownloadError("ОС запретила соединение с сервером файла (код %s); проверьте настройки сети и прокси" % code)
    if code in {errno.ETIMEDOUT, 10060}:
        return MediaDownloadError("Истекло время ожидания ответа сервера файла (код %s)" % code, retryable=True)
    if isinstance(reason, ConnectionError) or isinstance(code, int):
        suffix = " (код %s)" % code if isinstance(code, int) else ""
        return MediaDownloadError("Не удалось установить или сохранить соединение с сервером файла" + suffix, retryable=True)
    return MediaDownloadError("Не удалось скачать файл по сети; проверьте подключение и настройки прокси", retryable=True)


def public_https(url: str) -> str:
    """Reject unsupported schemes, embedded credentials and local addresses."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError()
    except ValueError:
        raise MediaDownloadError("Ожидался публичный HTTPS-адрес файла") from None
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise MediaDownloadError("Не удалось определить адрес сервера файла (DNS)", retryable=True) from None
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise MediaDownloadError("Локальный или служебный адрес файла запрещён")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def media_opener(*, system_tls: bool = False):
    if not system_tls:
        return urllib.request.build_opener(SafeRedirect())
    try:
        import truststore
    except ModuleNotFoundError:
        raise RuntimeError("Для --system-tls установите truststore в Python 3.10 или новее (python -m pip install truststore)") from None
    except ImportError:
        raise RuntimeError("Системная проверка TLS через truststore недоступна в этой версии ОС или Python") from None
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.set_alpn_protocols(["http/1.1"])
    return urllib.request.build_opener(SafeRedirect(), urllib.request.HTTPSHandler(context=context))


def _sniff_mime(head: bytes, reported: str, kind: str) -> str:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head[:4] in (b"II*\0", b"MM\0*"):
        return "image/tiff"
    if head[4:8] == b"ftyp":
        brands = head[8:32]
        if b"avif" in brands or b"avis" in brands:
            return "image/avif"
        if any(x in brands for x in (b"heic", b"heix", b"hevc", b"hevx", b"mif1")):
            return "image/heic"
        return "audio/mp4" if kind in {"audio", "voice"} else "video/mp4"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 255 and head[1] & 224 == 224):
        return "audio/mpeg"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm" if kind in {"audio", "voice"} else "video/webm"
    return reported or "application/octet-stream"


def _validate_payload(head: bytes, mime: str, kind: str, ext: str) -> str:
    stripped = head.lstrip().lower()
    is_html = mime in {"text/html", "application/xhtml+xml"} or stripped.startswith((b"<!doctype html", b"<html"))
    if not head:
        raise MediaDownloadError("Сервер вернул пустой файл")
    if is_html and not (kind == "doc" and _extension(ext) in {".html", ".htm"}):
        raise MediaDownloadError("Вместо файла сервер вернул HTML-страницу")
    if kind != "doc":
        if stripped.startswith(b"#extm3u"):
            raise MediaDownloadError("Получен HLS-плейлист вместо готового файла")
        if mime.startswith("text/") or mime in {"application/json", "application/xml", "text/xml"}:
            raise MediaDownloadError("Вместо медиа сервер вернул текст или ошибку")
        if stripped.startswith((b"<?xml", b"<error", b'{"error')) and mime != "image/svg+xml":
            raise MediaDownloadError("Вместо медиа сервер вернул текст или ошибку")
    detected = _sniff_mime(head, mime, kind) if kind != "doc" else mime
    if kind == "video" and (detected.startswith(("image/", "audio/")) or stripped.startswith(b"%pdf-")):
        raise MediaDownloadError("По адресу видео получен файл другого типа")
    return detected


def _stopped(stop) -> None:
    if stop is not None and stop.is_set():
        raise DownloadStopped()


def download_file(url: str, destination: Path, kind: str, *, expected_size: int = 0, expected_ext: str = "", stop=None, opener=None, timeout: int = 45) -> DownloadResult:
    """Stream one direct file, validate it, and atomically place the result."""
    _stopped(stop)
    public_https(url)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    opener = opener or media_opener()
    request = urllib.request.Request(url, headers={"User-Agent": "VKArchive/2"})
    temp = None
    try:
        with opener.open(request, timeout=timeout) as response:
            reported = response.headers.get_content_type()
            declared = response.headers.get("Content-Length")
            content_range = response.headers.get("Content-Range")
            head = response.read(8192)
            mime = _validate_payload(head, reported, kind, expected_ext or destination.suffix)
            original_ext = expected_ext or (destination.suffix if destination.suffix.lower() != ".bin" else "")
            suffix = choose_extension(kind, "", original_ext, mime, url)
            final = destination.with_suffix(suffix)
            digest, size = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(prefix="." + destination.stem[:48] + "-", suffix=".part", dir=str(destination.parent), delete=False) as output:
                temp = Path(output.name)
                chunk = head
                while chunk:
                    _stopped(stop)
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    chunk = response.read(256 * 1024)
            _stopped(stop)
            if declared:
                try:
                    length = int(declared)
                except (ValueError, TypeError):
                    raise MediaDownloadError("Сервер вернул некорректный размер файла") from None
                if size != length:
                    raise MediaDownloadError("Файл получен не полностью", retryable=True)
            if content_range or getattr(response, "status", None) == 206:
                match = re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range or "")
                if not match or int(match[1]) + 1 != size or int(match[2]) != size:
                    raise MediaDownloadError("Сервер вернул только часть файла", retryable=True)
            if expected_size and size != int(expected_size):
                raise MediaDownloadError("Размер документа не совпадает с размером, указанным API")
            os.replace(str(temp), str(final))
            temp = None
            return DownloadResult(final.resolve(), size, digest.hexdigest(), mime)
    except urllib.error.HTTPError as exc:
        if exc.fp is not None:
            with contextlib.suppress(OSError):
                exc.close()
        raise _http_error(exc.code) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, socket.gaierror, ssl.SSLError, http.client.HTTPException) as exc:
        raise _network_error(exc) from None
    except OSError as exc:
        if (getattr(exc, "winerror", 0) or 0) >= 10000 or exc.errno in {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN, errno.EPIPE}:
            raise _network_error(exc) from None
        raise
    finally:
        if temp is not None:
            with contextlib.suppress(OSError):
                temp.unlink()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_reference(source: Path, destination: Path, stop=None) -> str:
    """Make a visible source reference: hardlink when possible, otherwise copy."""
    _stopped(stop)
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        return "same"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        try:
            if os.path.samefile(str(source), str(destination)):
                return "existing"
        except OSError:
            pass
    handle, name = tempfile.mkstemp(prefix=".reference-", suffix=".tmp", dir=str(destination.parent))
    os.close(handle)
    temp = Path(name)
    temp.unlink()
    try:
        try:
            os.link(str(source), str(temp))
            method = "hardlink"
        except OSError:
            with source.open("rb") as incoming, temp.open("wb") as outgoing:
                for chunk in iter(lambda: incoming.read(256 * 1024), b""):
                    _stopped(stop)
                    outgoing.write(chunk)
            _stopped(stop)
            shutil.copystat(str(source), str(temp))
            method = "copy"
        _stopped(stop)
        os.replace(str(temp), str(destination))
        return method
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def _metadata(row: Mapping[str, Any]) -> Dict[str, Any]:
    value = row.get("metadata") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = {}
    return value if isinstance(value, dict) else {}


def _direct_sources(row: Mapping[str, Any]):
    primary = row.get("url")
    if row["kind"] != "video":
        return [("", primary)] if primary else []
    files = _metadata(row).get("files")
    options = []
    if isinstance(files, dict):
        for key, url in files.items():
            match = re.fullmatch(r"mp4_(\d{1,4})", key)
            if match and isinstance(url, str) and url:
                options.append((int(match[1]), url))
    sources = {}
    for resolution, url in sorted(options, reverse=True):
        sources.setdefault(url, "%sp" % resolution)
    if primary and primary not in sources:
        sources[primary] = "прямой адрес"
    return [(label, url) for url, label in sources.items()]


def _write_error(error):
    code = getattr(error, "winerror", None) or getattr(error, "errno", None)
    if code in {errno.ENOSPC, 112}:
        return MediaDownloadError("Недостаточно места на диске для сохранения файла")
    if code in {errno.ENAMETOOLONG, 206}:
        return MediaDownloadError("Слишком длинный путь к файлу; укажите более короткую папку архива через --out")
    if code in {errno.EACCES, errno.EPERM, 5, 32}:
        return MediaDownloadError("Не удалось записать файл; проверьте права на папку и не открыт ли файл другой программой")
    suffix = " (код %s)" % code if isinstance(code, int) else ""
    return MediaDownloadError("Ошибка записи файла" + suffix)


class Downloader:
    """Download collected assets, then materialize every readable source path."""
    def __init__(self, store, stop, use_ytdlp: bool = False, progress=None, *, opener=None):
        self.store, self.stop, self.use_ytdlp = store, stop, use_ytdlp
        self.progress = progress
        self.opener = opener or media_opener()

    def _sources(self, row):
        sources = self.store.media_sources(row["key"])
        if not sources:
            sources = [{"category": "unknown", "source_id": "unknown", "name": "Источник не указан", "ts": row.get("ts", 0), "path": ""}]
        return sorted(sources, key=lambda x: (SOURCE_PRIORITY.get(x.get("category", "unknown"), 99), str(x.get("source_id", ""))))

    def _path(self, row, source, ext="", mime=""):
        metadata = _metadata(row)
        relative = plan_path(row["kind"], source, key=row["key"], title=row.get("name") or metadata.get("title", ""), timestamp=row.get("ts", 0), ext=ext or row.get("ext") or metadata.get("ext", ""), mime=mime, url=row.get("url", ""), compact=len(str(self.store.root).encode("utf-16-le")) // 2 > 70)
        return self.store.root / relative

    def _existing(self, row):
        candidates = []
        if row.get("path"):
            candidates.append(self.store.root / row["path"])
        candidates.extend(self.store.root / x["path"] for x in self._sources(row) if x.get("path"))
        for path in candidates:
            if path.is_file() and path.stat().st_size == int(row.get("size", 0) or 0) and row.get("sha256"):
                if row.get("status") == "partial" and hash_file(path) != row["sha256"]:
                    continue
                return DownloadResult(path.resolve(), path.stat().st_size, row["sha256"], row.get("mime") or "application/octet-stream")
        if row.get("url"):
            done = self.store.rows("SELECT * FROM assets WHERE url=? AND key<>? AND status IN ('done','partial') AND path<>'' LIMIT 1", (row["url"], row["key"]))
            if done:
                other = done[0]
                path = self.store.root / other["path"]
                if path.is_file() and (not row.get("expected_size") or path.stat().st_size == row["expected_size"]):
                    return DownloadResult(path.resolve(), path.stat().st_size, other["sha256"], other["mime"])
        return None

    def _references(self, row, result):
        # Preserve a fully received file even if reference creation is interrupted.
        self.store.mark_media_done(row["key"], result.path.relative_to(self.store.root).as_posix(), result.size, result.sha256, result.mime)
        self.store.mark_media_error(row["key"], "partial", "Создание путей по источникам ещё не завершено")
        errors = []
        canonical = None
        for source in self._sources(row):
            _stopped(self.stop)
            path = self._path(row, source, ext=result.path.suffix, mime=result.mime)
            try:
                ensure_reference(result.path, path, stop=self.stop)
                relative = path.relative_to(self.store.root).as_posix()
                self.store.set_source_path(row["key"], source["category"], str(source.get("source_id", source.get("id", ""))), relative)
                if canonical is None:
                    canonical = path
            except OSError as exc:
                errors.append("%s/%s: %s" % (source.get("category"), source.get("source_id", ""), type(exc).__name__))
        canonical = canonical or result.path
        self.store.mark_media_done(row["key"], canonical.relative_to(self.store.root).as_posix(), result.size, result.sha256, result.mime)
        if errors:
            message = "Не созданы некоторые пути по источникам: " + "; ".join(errors)
            self.store.mark_media_error(row["key"], "partial", message)
            self.store.log("WARNING", "Медиа", message)
            return "partial"
        return "done"

    def _yt_dlp(self, row, destination):
        metadata = _metadata(row)
        url = metadata.get("player")
        if not self.use_ytdlp or row["kind"] != "video" or not url:
            raise MediaDownloadError(row.get("error") or "API не вернул прямой адрес файла")
        if metadata.get("live") or metadata.get("is_live"):
            raise MediaDownloadError("Текущая прямая трансляция не скачивается в архив")
        if importlib.util.find_spec("yt_dlp") is None:
            raise MediaDownloadError("Для --yt-dlp требуется отдельно установленный пакет yt-dlp")
        public_https(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        template = str(destination.with_suffix("")).replace("%", "%%") + ".%(ext)s"
        cmd = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-cache-dir", "--no-progress", "--no-warnings", "--socket-timeout", "30", "--retries", "2", "--fragment-retries", "2", "--format", "best[ext=mp4]/best", "--output", template, "--", url]
        env = {key: value for key, value in os.environ.items() if key.upper() != "VK_ACCESS_TOKEN"}
        with subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env) as process:
            while process.poll() is None:
                if self.stop.wait(0.5):
                    process.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=5)
                    if process.poll() is None:
                        process.kill()
                    raise DownloadStopped()
            if process.returncode:
                raise MediaDownloadError("yt-dlp не смог получить готовый файл видео")
        prefix = destination.stem + "."
        candidates = [p for p in destination.parent.iterdir() if p.is_file() and p.name.startswith(prefix) and p.suffix not in {".part", ".ytdl", ".tmp"}]
        if not candidates:
            raise MediaDownloadError("yt-dlp не создал готовый файл видео")
        path = max(candidates, key=lambda p: p.stat().st_size)
        with path.open("rb") as stream:
            mime = _validate_payload(stream.read(8192), mimetypes.guess_type(str(path))[0] or "application/octet-stream", "video", path.suffix)
        return DownloadResult(path.resolve(), path.stat().st_size, hash_file(path), mime)

    def _obtain(self, row):
        existing = self._existing(row)
        if existing is not None:
            return existing, True
        sources = self._sources(row)
        destination = self._path(row, sources[0])
        failures = []
        for label, url in _direct_sources(row):
            for attempt in range(3):
                _stopped(self.stop)
                try:
                    result = download_file(url, destination, row["kind"], expected_size=row.get("expected_size", 0), expected_ext=row.get("ext") or _metadata(row).get("ext", ""), stop=self.stop, opener=self.opener)
                    return result, False
                except MediaDownloadError as exc:
                    if not exc.retryable or attempt == 2:
                        failures.append((label + ": " if label else "") + str(exc))
                        break
                    if self.stop.wait(2 ** attempt):
                        raise DownloadStopped()
        if row["kind"] == "video" and self.use_ytdlp and _metadata(row).get("player"):
            try:
                return self._yt_dlp(row, destination), False
            except MediaDownloadError as exc:
                failures.append("Проигрыватель: " + str(exc))
        raise MediaDownloadError("; ".join(failures) or row.get("error") or "API не вернул адрес файла")

    def run(self):
        summary = {"downloaded": 0, "reused": 0, "failed": 0, "unavailable": 0, "partial": 0}
        rows = self.store.rows("SELECT * FROM assets ORDER BY key")
        for index, row in enumerate(rows, 1):
            _stopped(self.stop)
            self.store.meta("phase", "Файлы: %s/%s — %s" % (index, len(rows), row.get("name") or row["key"]))
            if self.progress is not None:
                self.progress(index, len(rows), row)
            if not row.get("path") and not _direct_sources(row) and not (self.use_ytdlp and row["kind"] == "video" and _metadata(row).get("player")):
                reason = row.get("error") or "API не вернул адрес файла"
                if row["kind"] == "video" and _metadata(row).get("player"):
                    reason = "Нет прямого адреса MP4. Для попытки через проигрыватель запустите загрузчик с --yt-dlp"
                self.store.mark_media_error(row["key"], "unavailable", reason)
                summary["unavailable"] += 1
                continue
            try:
                result, reused = self._obtain(row)
                state = self._references(row, result)
                summary["reused" if reused else "downloaded"] += 1
                if state == "partial":
                    summary["partial"] += 1
            except DownloadStopped:
                raise
            except MediaDownloadError as exc:
                self.store.mark_media_error(row["key"], "failed", str(exc))
                summary["failed"] += 1
            except OSError as exc:
                self.store.mark_media_error(row["key"], "failed", str(_write_error(exc)))
                summary["failed"] += 1
            except ValueError:
                self.store.mark_media_error(row["key"], "failed", "Некорректный адрес или параметры файла")
                summary["failed"] += 1
        return summary


def verify_files(store):
    """Check canonical files and every human-readable source reference."""
    cache, physical = {}, {}
    errors = []
    checked = 0
    assets = store.rows("SELECT * FROM assets WHERE status IN ('done','partial')")
    for row in assets:
        references = [("canonical", row.get("path", ""))]
        references.extend(("%s/%s" % (s["category"], s.get("source_id", "")), s.get("path", "")) for s in store.media_sources(row["key"]))
        for source, relative in references:
            checked += 1
            if not relative:
                errors.append({"key": row["key"], "source": source, "path": "", "error": "Путь не создан"})
                continue
            if relative not in cache:
                path = store.root / relative
                try:
                    resolved = path.resolve()
                    if store.root not in resolved.parents:
                        raise ValueError()
                    info = path.stat()
                    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) if info.st_ino else (str(resolved), info.st_size, info.st_mtime_ns)
                    if identity not in physical:
                        physical[identity] = (info.st_size, hash_file(path))
                    cache[relative] = physical[identity]
                except (OSError, ValueError):
                    cache[relative] = None
            if cache[relative] != (int(row.get("size", 0)), row.get("sha256")):
                errors.append({"key": row["key"], "source": source, "path": relative, "error": "Файл отсутствует или изменился"})
    result = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "checked_references": checked, "unique_paths": len(cache), "unique_files": len(physical), "errors": errors}
    target = store.root / "verify_report.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temp), str(target))
    return result
