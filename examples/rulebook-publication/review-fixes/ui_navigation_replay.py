"""Re-run the fixed public App case; preserve each chosen output filename."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

evidence = Path(__file__).resolve().parent
root = evidence.parents[2]
name = sys.argv[1] if len(sys.argv) == 2 else "ui-unresolved-navigation.after.json"
if Path(name).name != name or not name.endswith(".json"):
    raise SystemExit("Use one JSON filename inside the evidence directory")
output = evidence / name
if output.exists():
    raise SystemExit("Choose a new output filename; existing evidence is immutable")
scratch = root / "frontend/.cache/rulebook-review/navigation.test.tsx"
scratch.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(evidence / "ui-unresolved-navigation.case.tsx", scratch)
try:
    result = subprocess.run(
        [
            "npm.cmd" if os.name == "nt" else "npm",
            "test", "--", ".cache/rulebook-review/navigation.test.tsx",
            "--reporter=json", "--outputFile=" + str(output),
        ],
        cwd=root / "frontend",
        check=False,
    )
finally:
    if scratch.resolve().is_relative_to((root / "frontend/.cache").resolve()):
        scratch.unlink(missing_ok=True)
raise SystemExit(result.returncode)
