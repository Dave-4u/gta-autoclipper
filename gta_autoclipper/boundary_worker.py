"""Disposable Whisper worker: parent enforces remaining source wall and cancellation."""
import json
import os
import tempfile
import sys
from pathlib import Path
os.environ["GTA_LOW_CPU"]="1"
os.environ["WHISPER_ESCALATE"]="0"
from .analyze import get_word_timestamps

if __name__ == '__main__':
    source,start,end,destination,niche=sys.argv[1:]
    work=Path(destination).with_suffix('.work')
    work.mkdir(parents=True,exist_ok=True)
    tempfile.tempdir=str(work)
    words=get_word_timestamps(Path(source),float(start),float(end),model_name='tiny',language=None,niche_id=niche)
    Path(destination).write_text(json.dumps(words))
