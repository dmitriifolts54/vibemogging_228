"""Run directly from a checkout: python3 -m vibemogging."""

import argparse
from pathlib import Path

from .server import make_server
from .storage import UserError
from .workspace import Workspace

DEFAULT_WORKSPACE = Path.home() / "Vibemogging"
LEGACY_DATA_DIR = Path(".vibemogging")


def main():
    parser = argparse.ArgumentParser(description="Vibemogging — локальный видеоредактор с историей версий")
    parser.add_argument("--data-dir", type=Path, help="Открыть одну папку проекта без списка проектов (как в V1)")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                        help=f"Папка списка проектов, по умолчанию {DEFAULT_WORKSPACE}")
    parser.add_argument("--port", type=int, default=8765, help="Локальный порт, по умолчанию 8765")
    parser.add_argument("--preview-cache-mb", type=int, default=1024,
                        help="Лимит временных MP4-предпросмотров и экспортов на проект, МиБ (по умолчанию 1024)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("порт должен быть от 1 до 65535")
    if not 16 <= args.preview_cache_mb <= 1024 * 1024:
        parser.error("--preview-cache-mb: от 16 МиБ")
    cache = args.preview_cache_mb * 1024 * 1024
    try:
        if args.data_dir:
            server = make_server(args.data_dir, args.port, preview_cache=cache)
            location = f"Проект: {args.data_dir.resolve()}"
        else:
            workspace = Workspace(args.workspace)
            if (LEGACY_DATA_DIR / "state.json").is_file():
                # Keep the V1 default project visible in the new project list.
                workspace.register(LEGACY_DATA_DIR)
            server = make_server(port=args.port, workspace=workspace.root, preview_cache=cache)
            location = f"Проекты: {workspace.root}"
    except (UserError, OSError) as error:
        parser.exit(1, f"Не удалось запустить: {error}\n")
    print(f"Vibemogging: http://127.0.0.1:{args.port}", flush=True)
    print(location, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка…", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
