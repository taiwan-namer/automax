"""Bounded non-blocking writer queue so archive IO cannot block trading."""

from __future__ import annotations

import json
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .capacity import CapacityGuard


@dataclass
class WriteJob:
    kind: str
    payload: dict[str, Any]
    estimate_bytes: int = 512


class ArchiveWriter:
    """Background consumer; put() never blocks the strategy thread beyond a try/timeout=0."""

    def __init__(
        self,
        *,
        capacity: CapacityGuard,
        handlers: dict[str, Callable[[dict[str, Any]], None]],
        maxsize: int = 10_000,
        flush_interval_sec: float = 0.5,
        name: str = "replay_archive_writer",
    ) -> None:
        self.capacity = capacity
        self.handlers = handlers
        self.q: queue.Queue[Optional[WriteJob]] = queue.Queue(maxsize=maxsize)
        self.flush_interval_sec = flush_interval_sec
        self.name = name
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.stats = {
            "enqueued": 0,
            "written": 0,
            "dropped_full": 0,
            "rejected_capacity": 0,
            "handler_errors": 0,
            "bad_rows_skipped": 0,
        }
        self._meta = capacity.root / "_meta"
        self._meta.mkdir(parents=True, exist_ok=True)
        self._stats_path = self._meta / "writer_stats.json"
        self._gap_path = self._meta / "write_gaps.jsonl"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()
        self._heartbeat()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def _heartbeat(self) -> None:
        try:
            hb = {
                "ts": time.time(),
                "alive": self.is_alive(),
                "stats": dict(self.stats),
                "qsize": self.q.qsize(),
            }
            (self._meta / "writer_heartbeat.json").write_text(
                json.dumps(hb, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=timeout)

    def put(self, kind: str, payload: dict[str, Any], *, estimate_bytes: int = 512) -> bool:
        """Non-blocking enqueue. Returns False if not accepted (never raises into trading)."""
        try:
            ok, reason = self.capacity.allow_write(estimate_bytes)
            if not ok:
                self.stats["rejected_capacity"] += 1
                self._gap("CAPACITY_REJECT", kind=kind, reason=reason, payload_keys=list(payload)[:20])
                return False
            self.q.put_nowait(WriteJob(kind=kind, payload=payload, estimate_bytes=estimate_bytes))
            self.stats["enqueued"] += 1
            return True
        except queue.Full:
            self.stats["dropped_full"] += 1
            self.capacity.note_gap("queue_full")
            self._gap("QUEUE_FULL", kind=kind)
            return False
        except Exception as exc:  # noqa: BLE001 — must never break trading
            self.stats["handler_errors"] += 1
            self._gap("ENQUEUE_ERROR", kind=kind, error=str(exc))
            return False

    def _gap(self, code: str, **kw: Any) -> None:
        try:
            with self._gap_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"code": code, "ts": time.time(), **kw}, default=str) + "\n")
        except OSError:
            pass

    def _persist_stats(self) -> None:
        try:
            self._stats_path.write_text(json.dumps(self.stats, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.q.get(timeout=self.flush_interval_sec)
            except queue.Empty:
                self._persist_stats()
                self._heartbeat()
                continue
            if job is None:
                break
            try:
                handler = self.handlers.get(job.kind)
                if handler is None:
                    self.stats["bad_rows_skipped"] += 1
                    self._gap("UNKNOWN_KIND", kind=job.kind)
                else:
                    handler(job.payload)
                    self.stats["written"] += 1
            except Exception as exc:  # noqa: BLE001
                self.stats["handler_errors"] += 1
                self.capacity.note_gap("handler_error")
                self._gap(
                    "HANDLER_ERROR",
                    kind=job.kind,
                    error=str(exc),
                    traceback=traceback.format_exc()[-2000:],
                )
            finally:
                self.q.task_done()
                if self.stats["written"] % 50 == 0:
                    self._heartbeat()
        self._persist_stats()
        self._heartbeat()

    def drain_sync_for_tests(self, timeout: float = 5.0) -> None:
        """Test helper: wait until queue empty and settled (does not stop worker)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.q.unfinished_tasks == 0 and self.q.empty():
                time.sleep(0.05)
                if self.q.unfinished_tasks == 0 and self.q.empty():
                    return
            time.sleep(0.02)
