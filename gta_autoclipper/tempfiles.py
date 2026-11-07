"""FFmpeg/Gemini must never inherit an open Python temp-file handle on Windows."""
import os
import tempfile
from pathlib import Path


def closed_temp_path(*, suffix):
    Path(tempfile.gettempdir()).mkdir(parents=True,exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return Path(name)
