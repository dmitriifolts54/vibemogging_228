"""Run directly from a checkout: python3 -m vibemogging."""

import argparse
from pathlib import Path

from .server import make_server
from .storage import UserError


def main():
    parser = argparse.ArgumentParser(description="Vibemogging — локальный видеоредактор с историей версий")
    parser.add_argument("--data-dir", type=Path, default=Path(".vibemogging"), help="Папка видеорепозитория")
    parser.add_argument("--port", type=int, default=8765, help="Локальный порт, по умолчанию 8765")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("порт должен быть от 1 до 65535")
    try:
        server = make_server(args.data_dir, args.port)
    except (UserError, OSError) as error:
        parser.exit(1, f"Не удалось запустить: {error}\n")
    print(f"Vibemogging: http://127.0.0.1:{args.port}", flush=True)
    print(f"Данные: {args.data_dir.resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка…", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
