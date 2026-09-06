"""Independent subprocess client of the public journal API; no network transport."""

import json
import os
import sys
import time
from pathlib import Path

from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError

request = json.load(sys.stdin)
journal = GoCallJournal(Path(request["path"]), clock=lambda: 100.0)
if request.get("gate"):
    deadline = time.monotonic() + 10
    while not Path(request["gate"]).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("GATE_TIMEOUT")
        time.sleep(0.005)
results = []
for call in request["calls"]:
    try:
        result = journal.begin_call(
            "grant", call, binding=request["binding"], capability=request["capability"]
        )
        results.append({"call": call, "send_allowed": result["send_allowed"]})
    except GoJournalError as error:
        results.append({"call": call, "error": str(error)})
if request.get("crash_after_commit"):
    os._exit(23)
print(json.dumps(results))
