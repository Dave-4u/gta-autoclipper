"""Render fully edited 9:16 clips with blur fill, smart hook, captions, VO, loudnorm."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .analyze import Segment, get_word_timestamps
from .hooks import HookContext, HookResult, generate_hook
from .voiceover import make_intro_vo, make_voiceover

COLOR_ACTIVE = "H0000FFFF"
COLOR_DIM = "H00FFFFFF"

# SEE ARTIFACTS - full content too large for this intermediate step
