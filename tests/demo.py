"""Create an entirely synthetic archive for UI review, without VK or a token."""

import argparse
import contextlib
import hashlib
import io
import json
import struct
import threading
import wave
import zipfile
import zlib
from pathlib import Path

from tests.test_collector import FakeAPI
from vk_archive.collector import Collector
from vk_archive.download_cli import write_report
from vk_archive.media import Downloader, ensure_reference, plan_path
from vk_archive.store import Store


def png(seed, width=480, height=280):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    colors = ((41, 118, 171), (71, 143, 110), (152, 103, 133), (176, 124, 68))
    base = colors[seed % len(colors)]
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)
        for x in range(width):
            shade = (x // 40 + y // 40) % 2 * 14 + int(28 * y / height)
            scanlines.extend(min(255, component + shade) for component in base)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(scanlines), 6)) + chunk(b"IEND", b"")


def wav():
    result = io.BytesIO()
    with wave.open(result, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 8000)
    return result.getvalue()


def docx():
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        output.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        output.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Демонстрационный документ. Это искусственные данные.</w:t></w:r></w:p></w:body></w:document>')
    return result.getvalue()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="archives/demo")
    args = parser.parse_args(argv)
    root = Path(args.out).expanduser().resolve()
    if (root / "archive.sqlite3").exists():
        parser.error("Папка уже содержит архив. Для демо укажите новую папку --out")
    store = Store(root)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            if not Collector(FakeAPI(store), store, threading.Event()).run():
                raise RuntimeError("Не удалось создать демонстрационные данные")
        downloader = Downloader(store, threading.Event())
        for index, row in enumerate(store.rows("SELECT * FROM assets ORDER BY key")):
            if row["kind"] == "video":
                store.mark_media_error(row["key"], "unavailable", "В демо нет видеофайла; показана сохранённая миниатюра")
                continue
            if row["kind"] in {"voice", "audio"}:
                data, mime, ext = wav(), "audio/wav", ".wav"
            elif row["kind"] == "doc":
                data, mime, ext = docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"
            else:
                data, mime, ext = png(index), "image/png", ".png"
            sources = downloader._sources(row)
            path = root / plan_path(row["kind"], sources[0], key=row["key"], title=row["name"], timestamp=row["ts"], ext=ext, mime=mime)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            for source in sources:
                target = root / plan_path(row["kind"], source, key=row["key"], title=row["name"], timestamp=row["ts"], ext=ext, mime=mime)
                ensure_reference(path, target)
                store.set_source_path(row["key"], source["category"], source["source_id"], target.relative_to(root))
            store.mark_media_done(row["key"], path, len(data), digest, mime)
        store.meta("run_state", "finished_with_gaps")
        store.meta("phase", "Демонстрационный архив — искусственные данные")
        write_report(store)
        print("Демо создано без сетевых запросов: " + str(root))
        print("Просмотр: python view.py --out " + json.dumps(str(root), ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()
