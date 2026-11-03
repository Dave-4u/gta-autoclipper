"""Per-stage progress logging and soft timeouts for CPU-friendly runs."""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


LogFn = Callable[[str], None]


@dataclass
class StageInfo:
    name: str
    status: str = "pending"  # pending|running|ok|fail|timeout|skipped
    started_at: float | None = None
    ended_at: float | None = None
    detail: str = ""


@dataclass
class JobProgress:
    """Shared progress for CLI / web UI."""

    stages: list[StageInfo] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    current_stage: str = ""
    status: str = "idle"  # idle|running|done|error|stopped
    error: str = ""
    produced: list[str] = field(default_factory=list)
    started_at: float | None = None
    ended_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def log(self, msg: str) -> None:
        line = msg if isinstance(msg, str) else str(msg)
        ts = time.strftime("%H:%M:%S")
        stamped = f"[{ts}] {line}"
        print(stamped, flush=True)
        with self._lock:
            self.log_lines.append(stamped)
            if len(self.log_lines) > 800:
                self.log_lines = self.log_lines[-600:]

    def start_job(self) -> None:
        with self._lock:
            self.status = "running"
            self.error = ""
            self.produced = []
            self.stages = []
            self.log_lines = []
            self.started_at = time.time()
            self.ended_at = None
            self.current_stage = ""

    def finish_job(self, *, ok: bool = True, error: str = "") -> None:
        with self._lock:
            self.status = "done" if ok else "error"
            self.error = error or ""
            self.ended_at = time.time()
            self.current_stage = ""

    def begin(self, name: str, detail: str = "") -> StageInfo:
        st = StageInfo(name=name, status="running", started_at=time.time(), detail=detail)
        with self._lock:
            self.stages.append(st)
            self.current_stage = name
        self.log(f"[stage] ▶ {name}" + (f" — {detail}" if detail else ""))
        return st

    def end(self, name: str, *, status: str = "ok", detail: str = "") -> None:
        with self._lock:
            for st in reversed(self.stages):
                if st.name == name and st.status == "running":
                    st.status = status
                    st.ended_at = time.time()
                    if detail:
                        st.detail = detail
                    elapsed = (st.ended_at - (st.started_at or st.ended_at))
                    self.log(f"[stage] ■ {name} ({status}, {elapsed:.1f}s)" + (f" — {detail}" if detail else ""))
                    if self.current_stage == name:
                        self.current_stage = ""
                    return
        self.log(f"[stage] ■ {name} ({status})" + (f" — {detail}" if detail else ""))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            stages = []
            for s in self.stages:
                elapsed = None
                if s.started_at:
                    end = s.ended_at or time.time()
                    elapsed = round(end - s.started_at, 1)
                stages.append(
                    {
                        "name": s.name,
                        "status": s.status,
                        "detail": s.detail,
                        "elapsed_s": elapsed,
                    }
                )
            elapsed_job = None
            if self.started_at:
                end = self.ended_at or time.time()
                elapsed_job = round(end - self.started_at, 1)
            return {
                "status": self.status,
                "error": self.error,
                "current_stage": self.current_stage,
                "stages": stages,
                "log": "\n".join(self.log_lines[-200:]),
                "produced": list(self.produced),
                "elapsed_s": elapsed_job,
            }


def run_with_timeout(
    fn: Callable[[], Any],
    *,
    seconds: float,
    label: str,
    progress: JobProgress | None = None,
) -> tuple[bool, Any, str]:
    """
    Run fn in a daemon thread. On timeout return (False, None, reason).
    Note: underlying work may continue briefly (cannot hard-kill pure Python),
    but the caller should skip ahead / fail the job.
    """
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["result"] = fn()
            box["ok"] = True
        except Exception as e:
            box["ok"] = False
            box["error"] = str(e)
            box["tb"] = traceback.format_exc()

    t = threading.Thread(target=worker, daemon=True, name=f"stage-{label}")
    t.start()
    t.join(timeout=max(1.0, float(seconds)))
    if t.is_alive():
        msg = f"{label} timed out after {int(seconds)}s"
        if progress:
            progress.log(f"[timeout] {msg}")
        return False, None, msg
    if not box.get("ok", False):
        err = str(box.get("error") or "unknown error")
        if progress:
            progress.log(f"[error] {label}: {err}")
        return False, None, err
    return True, box.get("result"), ""
