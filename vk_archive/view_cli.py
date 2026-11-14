"""Start only the local archive viewer; never authenticate or call VK."""

import argparse
import sys
import webbrowser

from . import __version__
from .server import create_server
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description="Открыть сохранённый архив в локальном браузере без токена и интернета.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--out", default="archives/default", help="Папка готового архива")
    parser.add_argument("--port", type=int, default=0, help="Порт на 127.0.0.1; по умолчанию выбирается свободный")
    parser.add_argument("--no-browser", action="store_true", help="Только напечатать адрес, не открывая браузер автоматически")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("Порт должен быть от 0 до 65535")
    store = server = None
    try:
        store = Store(args.out, create=False)
        server = create_server(store, args.port)
        url = "http://127.0.0.1:%s/" % server.server_port
        print("Архив: " + str(store.root))
        print("Просмотр: " + url, flush=True)
        print("Остановка просмотрщика — Ctrl+C. Загрузчик запускается отдельно.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nПросмотрщик остановлен.")
    except (OSError, RuntimeError) as exc:
        print("Ошибка: " + str(exc), file=sys.stderr)
        return 1
    finally:
        if server:
            server.server_close()
        if store:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
