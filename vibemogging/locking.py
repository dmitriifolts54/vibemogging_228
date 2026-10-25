"""Hold an OS lock for the lifetime of a server using a data directory."""

import os
from pathlib import Path

from .storage import UserError


class DirectoryLock:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.file = (root / "process.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise UserError("Эта папка данных уже открыта другим процессом. Выберите другой --data-dir.") from error

    def close(self):
        if not self.file.closed:
            self.file.close()
