"""Bootstrap: rebuild edit.py from base64 chunks then re-exec real module."""
from pathlib import Path
import base64
import sys

here = Path(__file__).resolve().parent
parts = sorted(here.glob("_edit_b64_*.txt"))
if not parts:
    raise SystemExit("missing _edit_b64_*.txt chunks — run git pull --force")
data = "".join(p.read_text(encoding="ascii") for p in parts)
text = base64.b64decode(data.encode("ascii")).decode("utf-8")
target = here / "edit.py"
# Avoid rewrite loop: only rewrite if still bootstrap
if "Bootstrap: rebuild" in target.read_text(encoding="utf-8")[:200]:
    target.write_text(text, encoding="utf-8")
    for p in parts:
        p.unlink(missing_ok=True)
    print("[edit] restored full edit.py from chunks; re-run your command")
    sys.modules.pop("gta_autoclipper.edit", None)
# Load real implementation
exec(compile(text, str(target), "exec"), globals())
