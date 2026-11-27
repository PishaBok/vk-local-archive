"""Command-line orchestration, deliberately independent of the viewer."""

import argparse
import contextlib
import csv
import getpass
import hashlib
import json
import os
import signal
import shutil
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .api import API
from .media import DownloadStopped
from .store import Store, now

DEFAULT_OUT = "archives/default"


@contextlib.contextmanager
def export_lock(root):
    handle = (Path(root) / "export.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Для этой папки уже запущен загрузчик") from None
    try:
        yield
    finally:
        handle.close()


def read_token(hidden=False, debug=False):
    raw = os.environ.get("VK_ACCESS_TOKEN")
    if raw is None:
        raw = getpass.getpass("Токен VK (скрытый ввод): ") if hidden else input("Токен VK (видимый ввод): ")
    token = raw.strip()
    if debug:
        print("Версия %s; Python %s" % (__version__, sys.version.split()[0]))
        print("Длина ввода: %s; передаваемого токена: %s" % (len(raw), len(token)))
        print("Отпечаток SHA-256: " + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16])
        unusual = [(index, "U+%04X" % ord(char)) for index, char in enumerate(token) if ord(char) < 33 or ord(char) > 126]
        print("Посторонние символы: " + (str(unusual) if unusual else "нет"))
    elif not token or "access_token=" in token or "://" in token:
        raise ValueError("Введите только значение пользовательского токена, без полного URL и access_token=")
    return token


def write_report(store):
    target = store.root / "report.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(store.report(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temporary), str(target))
    # One row per source makes the readable on-disk layout searchable as a table.
    index = store.root / "media_index.csv"
    temporary = index.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(("type", "title", "source", "source_name", "source_id", "relative_path", "size", "sha256", "status", "error"))
        rows = store.rows("SELECT a.kind,a.name title,a.size,a.sha256,a.status,a.error,s.category,s.name source_name,s.source_id,s.path FROM assets a LEFT JOIN asset_sources s ON s.asset_key=a.key ORDER BY a.kind,s.category,s.name,a.name")
        for row in rows:
            values = [row[key] for key in ("kind", "title", "category", "source_name", "source_id", "path", "size", "sha256", "status", "error")]
            writer.writerow([("'" + value) if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value for value in values])
    os.replace(str(temporary), str(index))


@contextlib.contextmanager
def console_color():
    if not sys.stdout.isatty() or os.environ.get("TERM") == "dumb" or "NO_COLOR" in os.environ:
        yield False
        return
    if os.name != "nt":
        yield True
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    handle = msvcrt.get_osfhandle(sys.stdout.fileno())
    original = wintypes.DWORD()
    enabled = kernel.GetConsoleMode(handle, ctypes.byref(original)) and kernel.SetConsoleMode(handle, original.value | 0x0004)
    try:
        yield bool(enabled)
    finally:
        if enabled:
            kernel.SetConsoleMode(handle, original.value)


def completion_banner(store, gaps, metadata_only, elapsed):
    saved = dict(store.db().execute(
        "SELECT kind, COUNT(*) FROM assets WHERE status IN ('done','partial') AND path<>'' GROUP BY kind"
    ))
    minutes, seconds = divmod(int(elapsed), 60)
    title = "СБОР МЕТАДАННЫХ ЗАВЕРШЁН" if metadata_only else "ВЫГРУЗКА ЗАВЕРШЕНА"
    if gaps:
        title += " С ПРОПУСКАМИ"
    width = min(80, max(len(title) + 4, shutil.get_terminal_size().columns - 1))
    border = "=" * width
    with console_color() as color:
        start, reset = ("\033[1;92m", "\033[0m") if color else ("", "")
        print("\n" + start + border + "\n" + title.center(width) + "\n" + border + reset, flush=True)
    print("Время: %d мин %02d сек | Сохранённых вложений: %d" % (minutes, seconds, sum(saved.values())))
    print("Фото: %d | Видео: %d | Документы: %d | Голосовые: %d" % tuple(saved.get(kind, 0) for kind in ("photo", "video", "doc", "voice")))
    if gaps:
        print("Разделов и вложений с пропусками: %d. Подробности в report.json." % gaps)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Загрузить данные VK API и файлы в локальный архив. Просмотр: отдельная команда view.py.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--out", default=DEFAULT_OUT, help="Папка архива (по умолчанию archives/default)")
    parser.add_argument("--refresh", action="store_true", help="Повторить обход API с начала, сохранив готовые файлы")
    parser.add_argument("--files-only", action="store_true", help="Скачать/восстановить файлы по уже сохранённым данным, без токена")
    parser.add_argument("--metadata-only", action="store_true", help="Получить данные API и очередь файлов, не скачивая медиа")
    parser.add_argument("--verify", action="store_true", help="Проверить файлы и их копии/ссылки по SHA-256, без токена")
    parser.add_argument("--hidden-token", action="store_true", help="Скрывать ввод токена; по умолчанию используется обычный ввод")
    parser.add_argument("--debug-token", action="store_true", help="Показать длину/отпечаток токена и пропустить локальную проверку формата")
    parser.add_argument("--api-version", default="5.199")
    parser.add_argument("--system-tls", action="store_true", help="Для прямого скачивания файлов проверять HTTPS через truststore (Python 3.10+)")
    parser.add_argument("--yt-dlp", action="store_true", help="Дополнительно попробовать скачать потоковое видео через установленный yt-dlp")
    args = parser.parse_args(argv)
    if sum((args.files_only, args.metadata_only, args.verify)) > 1:
        parser.error("Выберите один режим: --files-only, --metadata-only или --verify")
    if args.refresh and (args.files_only or args.verify):
        parser.error("--refresh используется только вместе с обходом API")
    if args.system_tls and (args.metadata_only or args.verify):
        parser.error("--system-tls используется только при скачивании файлов")
    if (args.files_only or args.verify) and not (Path(args.out).expanduser() / "archive.sqlite3").is_file():
        parser.error("Архив не найден. Сначала выполните обычную выгрузку или укажите --out")
    stop = threading.Event()
    previous = None
    store = None
    try:
        # Imports are here so the viewer has no dependency on collector startup.
        from .collector import Collector
        from .media import Downloader, media_opener, verify_files
        opener = media_opener(system_tls=True) if args.system_tls else None
        token = "" if args.files_only or args.verify else read_token(args.hidden_token, args.debug_token)
        store = Store(args.out, create=not args.verify)
        print("Архив: " + str(store.root), flush=True)
        if args.verify:
            result = verify_files(store)
            target = store.root / "verify_report.json"
            errors = result.get("errors", [])
            print("Проверка завершена. Ошибок: %s. Отчёт: %s" % (len(errors), target))
            if not result.get("checked_references"):
                print("Готовых файлов для проверки нет.")
                return 2
            return 2 if errors else 0
        if args.files_only and not store.meta("owner_id"):
            raise RuntimeError("В архиве ещё нет данных аккаунта. Сначала выполните обычную загрузку с рабочим токеном.")
        with export_lock(store.root):
            started = time.monotonic()
            previous = signal.getsignal(signal.SIGINT)
            def interrupt(signum, frame):
                if stop.is_set():
                    raise KeyboardInterrupt()
                print("\nОстанавливаю загрузку; записанные страницы и файлы сохраняются…", flush=True)
                stop.set()
            signal.signal(signal.SIGINT, interrupt)
            api_ok = True
            if not args.files_only:
                api_ok = Collector(API(token, store, stop, args.api_version), store, stop, refresh=args.refresh).run()
            if api_ok and not stop.is_set() and not args.metadata_only:
                store.meta("phase", "Загрузка медиафайлов")
                def progress(index, total, row):
                    print("Медиа %s/%s: %s" % (index, total, row.get("name") or row["key"]), flush=True)
                Downloader(store, stop, use_ytdlp=args.yt_dlp, progress=progress, opener=opener).run()
            if stop.is_set():
                store.meta("run_state", "interrupted")
                store.meta("phase", "Загрузка прервана; следующий запуск продолжит работу")
                write_report(store)
                return 130
            if not api_ok:
                store.meta("phase", "Сбор данных остановлен с ошибкой; проверьте report.json")
                write_report(store)
                return 1
            gaps = store.db().execute("SELECT COUNT(*) FROM tasks WHERE status IN ('error','partial')").fetchone()[0]
            if not args.metadata_only:
                gaps += store.db().execute("SELECT COUNT(*) FROM assets WHERE status<>'done'").fetchone()[0]
            store.meta("run_state", "metadata_finished" if args.metadata_only else ("finished_with_gaps" if gaps else "finished"))
            store.meta("finished_at", now())
            store.meta("phase", "Обход завершён с пропусками; проверьте отчёт" if gaps else "Загрузка завершена")
            write_report(store)
            print("Отчёт: " + str(store.root / "report.json"))
            print("Просмотр: запустите view.py с той же папкой архива.")
            completion_banner(store, gaps, args.metadata_only, time.monotonic() - started)
            return 2 if gaps else 0
    except (KeyboardInterrupt, DownloadStopped):
        if store and not args.verify:
            store.meta("run_state", "interrupted")
            store.meta("phase", "Загрузка прервана; следующий запуск продолжит работу")
            write_report(store)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print("Ошибка: " + str(exc), file=sys.stderr)
        return 1
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
