"""Track yt-dlp/ffmpeg child processes so timeouts can actually kill them."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Sequence

_lock = threading.Lock()
_procs: set[subprocess.Popen] = set()
_cancel = threading.Event()


def cancel_event() -> threading.Event:
    return _cancel


def request_cancel() -> None:
    _cancel.set()
    kill_tracked()


def clear_cancel() -> None:
    _cancel.clear()


def cancelled() -> bool:
    return _cancel.is_set()


def register(proc: subprocess.Popen) -> None:
    with _lock:
        _procs.add(proc)


def unregister(proc: subprocess.Popen) -> None:
    with _lock:
        _procs.discard(proc)


def _kill_one(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform.startswith("win"):
            # Kill process tree on Windows
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def kill_tracked() -> int:
    with _lock:
        procs = list(_procs)
    n = 0
    for p in procs:
        if p.poll() is None:
            _kill_one(p)
            n += 1
        unregister(p)
    if n:
        print(f"[procutil] killed {n} child process tree(s)", flush=True)
    return n


def run_tracked(
    cmd: Sequence[str],
    *,
    timeout: float | None = None,
    text: bool = True,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """subprocess.run-like helper that registers the child for hard cancel."""
    if cancelled():
        return subprocess.CompletedProcess(cmd, 1, stdout="" if text else b"", stderr="cancelled")

    popen_kwargs: dict[str, Any] = dict(kwargs)
    popen_kwargs.setdefault("stdout", subprocess.PIPE)
    popen_kwargs.setdefault("stderr", subprocess.PIPE)
    if text:
        popen_kwargs.setdefault("text", True)
    # New process group so we can kill the tree (Unix)
    if not sys.platform.startswith("win"):
        popen_kwargs.setdefault("start_new_session", True)
    else:
        popen_kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    proc = subprocess.Popen(cmd, **popen_kwargs)
    register(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_one(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = ("" if text else b""), ("timeout" if text else b"timeout")
            raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
        # Poll cancel during long communicates is handled by timeout; also check after
        if cancelled() and proc.returncode == 0:
            # cancelled mid-flight but process finished — ok
            pass
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    finally:
        unregister(proc)


def run_with_timeout_cancelable(
    fn,
    *,
    seconds: float,
    label: str,
    progress=None,
    on_timeout=None,
) -> tuple[bool, Any, str]:
    """Run fn in a daemon thread; on timeout set cancel + kill children."""
    import traceback
    from typing import Any as _Any

    box: dict[str, _Any] = {}

    def worker() -> None:
        try:
            box["result"] = fn()
            box["ok"] = True
        except Exception as e:
            box["ok"] = False
            box["error"] = str(e)
            box["tb"] = traceback.format_exc()

    clear_cancel()
    t = threading.Thread(target=worker, daemon=True, name=f"stage-{label}")
    t.start()
    t.join(timeout=max(1.0, float(seconds)))
    if t.is_alive():
        msg = f"{label} timed out after {int(seconds)}s"
        if progress:
            progress.log(f"[timeout] {msg}")
        request_cancel()
        if on_timeout:
            try:
                on_timeout()
            except Exception:
                pass
        # Brief wait for workers to notice cancel
        t.join(timeout=8)
        return False, box.get("result"), msg
    if not box.get("ok", False):
        err = str(box.get("error") or "unknown error")
        if progress:
            progress.log(f"[error] {label}: {err}")
        return False, None, err
    return True, box.get("result"), ""
