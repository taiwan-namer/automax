"""Cross-platform exclusive file lock."""
from __future__ import annotations

from typing import TextIO

try:
    import msvcrt  # type: ignore

    def lock_ex_nb(fh: TextIO) -> None:
        fh.seek(0)
        if fh.read(1) == "":
            fh.write("0")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def unlock(fh: TextIO) -> None:
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

except ImportError:
    import fcntl  # type: ignore

    def lock_ex_nb(fh: TextIO) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(fh: TextIO) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
