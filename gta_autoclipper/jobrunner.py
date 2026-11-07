"""Run Cutroom jobs as a separate CLI process (never inside the web server).

- stdout/stderr drained continuously by a reader thread into a ring buffer (no pipe deadlock)
- status snapshot is lock-light and never waits on the job
- watchdog logs "no progress for N min" and enforces a hard timeout
- stop kills the whole process tree (taskkill /T on Windows, process group elsewhere)
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

_STAGE_RX = re.compile(r"^\[(discover|download|analyze|analyze-light|edit|montage|ranking|review|transcribe|"
                       r"intro-vo|voiceover|watermark|layout|auto|run|stage|ffmpeg|perf|job|watchdog)\]")
_FFMPEG_PROG_RX = re.compile(r"^\[ffmpeg\].*(?:\d+%|t=\d+|done in)")
_OUT_RX = re.compile(r"((?:[A-Za-z]:)?[^\s'\"]+\.mp4)\b")


class Job:
    def __init__(self) -> None:
        self.lines: deque[str] = deque(maxlen=1500)
        self.status = "idle"
        self.error = ""
        self.stage = ""
        self.produced: list[str] = []
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.last_output_at: float = 0.0
        self.proc: subprocess.Popen | None = None
        self.cmd: list[str] = []
        self._stop_requested = False

    # --- lifecycle -------------------------------------------------------
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str], *, cwd: Path, hard_timeout_s: float, stall_min: float) -> None:
        if self.running():
            raise RuntimeError("A job is already running")
        self.lines.clear()
        self.status, self.error, self.stage = "running", "", "starting"
        self.produced = []
        self.started_at, self.ended_at = time.time(), None
        self.last_output_at = time.time()
        self._stop_requested = False
        self.cmd = [sys.executable, "-u", "-m", "gta_autoclipper", *args]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        kw: dict[str, Any] = {}
        if sys.platform.startswith("win"):
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kw["start_new_session"] = True
        self._add(f"[job] $ {' '.join(a if ' ' not in a else repr(a) for a in self.cmd[2:])}")
        self.proc = subprocess.Popen(
            self.cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **kw,
        )
        threading.Thread(target=self._reader, daemon=True, name="job-reader").start()
        threading.Thread(target=self._watchdog, args=(hard_timeout_s, stall_min), daemon=True,
                         name="job-watchdog").start()

    def stop(self, reason: str = "stop requested") -> None:
        self._stop_requested = True
        self._add(f"[job] {reason} — killing process tree")
        self._kill_tree()

    def _kill_tree(self) -> None:
        p = self.proc
        if not p or p.poll() is not None:
            return
        try:
            if sys.platform.startswith("win"):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, check=False)
            else:
                os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # --- threads ---------------------------------------------------------
    def _add(self, line: str) -> None:
        self.lines.append(f"[{time.strftime('%H:%M:%S')}] {line}")

    def _reader(self) -> None:
        p = self.proc
        assert p and p.stdout
        for raw in p.stdout:  # drains continuously → child never blocks on a full pipe
            line = raw.rstrip("\r\n")
            if not line:
                continue
            self.last_output_at = time.time()
            self._add(line)
            m = _STAGE_RX.match(line)
            if m:
                self.stage = m.group(1)
            # ffmpeg -progress ticks count as progress (don't false-stall during long encodes)
            if _FFMPEG_PROG_RX.match(line):
                self.last_output_at = time.time()
                if "ffmpeg" not in (self.stage or ""):
                    self.stage = "ffmpeg"
            st = line.strip()
            is_final = st.endswith(".mp4") and os.path.isabs(st) and "_montage" not in st
            if " wrote " in line or line.startswith(("[auto] wrote", "[run] wrote")) or is_final:
                mm = _OUT_RX.search(st)
                if mm and mm.group(1) not in self.produced and "_montage" not in mm.group(1):
                    self.produced.append(mm.group(1))
        rc = p.wait()
        self.ended_at = time.time()
        self.stage = ""
        if self._stop_requested:
            self.status, self.error = "stopped", "stopped"
        elif rc == 0 and self.produced:
            self.status = "done"
        elif rc == 0:
            self.status, self.error = "error", "Finished but no clip was produced (see log)"
        else:
            self.status, self.error = "error", f"Job process exited with code {rc}"
        self._add(f"[job] finished status={self.status} rc={rc} files={len(self.produced)}")

    def _watchdog(self, hard_timeout_s: float, stall_min: float) -> None:
        warned_at = 0.0
        while self.running():
            time.sleep(5)
            now = time.time()
            idle = now - self.last_output_at
            if stall_min > 0 and idle > stall_min * 60 and now - warned_at > stall_min * 60:
                warned_at = now
                self._add(f"[watchdog] no progress for {idle / 60:.1f} min (stage={self.stage}) — "
                          "still running; press ABORT to kill it")
            if hard_timeout_s > 0 and self.started_at and now - self.started_at > hard_timeout_s:
                self.stop(f"hard timeout {hard_timeout_s / 60:.0f} min")
                return

    # --- snapshot (no locks; deque copy is atomic enough for display) ----
    def snapshot(self) -> dict[str, Any]:
        lines = list(self.lines)[-200:]
        end = self.ended_at or time.time()
        el = round(end - self.started_at, 1) if self.started_at else None
        return {
            "status": self.status,
            "error": self.error,
            "current_stage": self.stage,
            "stages": ([{"name": self.stage, "status": "running", "detail": "", "elapsed_s": el}]
                       if self.stage and self.status == "running" else []),
            "log": "\n".join(lines),
            "produced": list(self.produced),
            "elapsed_s": el,
            "idle_s": round(time.time() - self.last_output_at, 1) if self.running() else 0,
            "pid": self.proc.pid if self.proc else None,
        }


JOB = Job()


def cli_args_for(req: Any, config_path: str | None) -> list[str]:
    mode = (getattr(req, "mode", "single") or "single").lower()
    niche = str(getattr(req, "niche", "anime") or "anime")
    from .planning import automatic_plan
    plan = automatic_plan(mode, niche)
    a: list[str] = []
    if config_path:
        a += ["--config", str(config_path)]
    if mode == "ranking":
        a += ["--ranking", "--niche", niche, "--rank-n", "5"]
        if (getattr(req, "topic", "") or "").strip():
            a += ["--topic", req.topic.strip()]
        a.append("--automatic-plan")
        return a
    if mode in ("autopilot", "auto"):
        a += ["--auto", "--niche", niche, "--clips-per-niche", str(plan.output_target),
              "--max-videos", str(plan.source_limit)]
    else:
        a += ["--once", "--niche", niche, "--max-videos", str(plan.source_limit),
              "--max-clips", str(plan.clips_per_video)]
        if not getattr(req, "captions", True):
            a.append("--no-captions")
    if getattr(req, "fast", True):
        a.append("--fast")
    if not getattr(req, "voiceover", True):
        a.append("--no-voiceover")
    return a
