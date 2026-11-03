"""Local Cutroom web UI (FastAPI) — Gradio removed."""

from __future__ import annotations


def launch_ui(
    *,
    config_path: str | None = None,
    host: str = "127.0.0.1",
    port: int = 7860,
    open_browser: bool = True,
    share: bool = False,  # kept for CLI compat; unused
    prevent_thread_lock: bool = False,  # kept for CLI compat; unused
) -> None:
    from .webapp import launch_ui as _launch

    if share:
        print("[ui] share links are not used by Cutroom (local only)")
    _launch(
        config_path=config_path,
        host=host,
        port=port,
        open_browser=open_browser,
    )


if __name__ == "__main__":
    launch_ui()
