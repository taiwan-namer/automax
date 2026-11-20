from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


@dataclass
class JsonlReadResult:
    rows: List[Dict[str, Any]] = field(default_factory=list)
    bytes_read: int = 0
    lines_ok: int = 0
    truncated_tail: bool = False
    parse_errors: int = 0
    path: str = ""
    mtime: Optional[float] = None


def read_jsonl_bounded(
    path: Path,
    *,
    max_bytes: Optional[int] = None,
    skip_incomplete_tail: bool = True,
) -> JsonlReadResult:
    """Read JSONL up to file size at open; incomplete last line deferred with warning."""
    out = JsonlReadResult(path=str(path))
    if not path.exists():
        return out
    st = path.stat()
    out.mtime = st.st_mtime
    size = st.st_size if max_bytes is None else min(st.st_size, max_bytes)
    with path.open("rb") as f:
        data = f.read(size)
    out.bytes_read = len(data)
    if not data:
        return out
    text = data.decode("utf-8", errors="replace")
    if skip_incomplete_tail and not text.endswith("\n") and "\n" in text:
        # drop trailing partial line
        text, _partial = text.rsplit("\n", 1)
        out.truncated_tail = True
        text += "\n"
    elif skip_incomplete_tail and not text.endswith("\n") and text.strip():
        # single incomplete line
        out.truncated_tail = True
        return out
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            out.parse_errors += 1
            continue
        if isinstance(row, dict):
            out.rows.append(row)
            out.lines_ok += 1
    return out


def fill_dedupe_key(f: Dict[str, Any]) -> Tuple:
    return (
        f.get("trade_id"),
        f.get("event"),
        f.get("recorded_at"),
        round(float(f.get("qty") or 0), 10),
        round(float(f.get("price") or 0), 12),
        f.get("exit_reason"),
        round(float(f.get("fee_u") or 0), 10),
        round(float(f.get("net_u") or f.get("realized_net_u") or 0), 10),
        f.get("book_ts"),
    )


def dedupe_fills(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    seen = set()
    out = []
    dup = 0
    for r in rows:
        k = fill_dedupe_key(r)
        if k in seen:
            dup += 1
            continue
        seen.add(k)
        out.append(r)
    out.sort(key=lambda x: (x.get("recorded_at") or "", x.get("event") or ""))
    return out, dup


class FileLock:
    """Exclusive flock for report generation (non-trading)."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: Optional[int] = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._fd)
            self._fd = None
            raise RuntimeError(f"report lock busy: {self.path}") from exc
        return self

    def __exit__(self, *args) -> None:
        if self._fd is not None:
            try:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")
