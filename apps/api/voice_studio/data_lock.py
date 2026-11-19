"""An OS lock, rather than a PID file, prevents two services owning one data root."""
from pathlib import Path
import os

from .errors import AppError


class DataRootLock:
    def __init__(self, root: Path):
        self.file = (root / ".studio.lock").open("a+b")
        try:
            self.file.seek(0, 2)
            if self.file.tell() == 0:
                self.file.write(b"\0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise AppError(409, "该数据目录已有声稿服务运行，请使用现有服务或另选数据目录") from error

    def close(self):
        if self.file.closed:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
