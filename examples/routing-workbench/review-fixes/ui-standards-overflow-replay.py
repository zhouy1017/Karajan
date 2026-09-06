"""Replay the immutable public UI overflow case without changing product files."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

directory = Path(__file__).resolve().parent
root = directory.parents[2]
name = sys.argv[1] if len(sys.argv) == 2 else "ui-standards-overflow.after.json"
if Path(name).name != name or not name.endswith(".json"):
    raise SystemExit("Provide one new JSON output filename")
output = directory / name
if output.exists():
    raise SystemExit("Existing evidence must be preserved")
scratch = root / "frontend/.cache/routing-standards/overflow.test.tsx"
scratch.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(directory / "ui-standards-overflow.case.tsx", scratch)
try:
    result = subprocess.run(
        [
            "npm.cmd" if os.name == "nt" else "npm",
            "test", "--", ".cache/routing-standards/overflow.test.tsx",
            "--reporter=json", "--outputFile=" + str(output),
        ],
        cwd=root / "frontend", check=False,
    )
finally:
    if scratch.resolve().is_relative_to((root / "frontend/.cache").resolve()):
        scratch.unlink(missing_ok=True)
raise SystemExit(result.returncode)
