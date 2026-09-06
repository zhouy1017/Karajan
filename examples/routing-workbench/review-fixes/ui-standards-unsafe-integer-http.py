"""Compare the immutable input with actual JavaScript roundtrip bytes over HTTP."""

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

directory = Path(__file__).resolve().parent
root = directory.parents[2]
sys.path[:0] = [str(root / "backend"), str(root / "tests/web")]

from fastapi.testclient import TestClient  # noqa: E402
from test_rulebook_http import ORIGIN, login, publication_case  # noqa: E402

name = sys.argv[1] if len(sys.argv) == 2 else "ui-standards-unsafe-integer.http-comparison.json"
if Path(name).name != name or not name.endswith(".json"):
    raise SystemExit("Provide one new JSON output filename")
output = directory / name
if output.exists():
    raise SystemExit("Existing evidence must be preserved")
original = (directory / "ui-standards-unsafe-integer.input.json").read_bytes()
rounded = subprocess.run(
    [
        "node", "-e",
        "process.stdout.write(JSON.stringify(JSON.parse(require('fs').readFileSync(0,'utf8'))))",
    ],
    input=original, capture_output=True, check=True,
).stdout
assert rounded.decode() == (
    directory / "ui-standards-unsafe-integer.roundtrip.json"
).read_text(encoding="utf-8").strip()
results = {}
with tempfile.TemporaryDirectory(prefix="karajan-ui-number-review-") as temporary:
    case = publication_case.__wrapped__(Path(temporary))
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = {**login(client), "Content-Type": "application/json"}
        for label, body in [("original", original), ("actual_javascript_roundtrip", rounded)]:
            response = client.post(
                f"/v1/projects/{case['project']['id']}/rulebook/simulate",
                content=body, headers=headers,
            )
            result = response.json()["result"]
            results[label] = {
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "status": response.status_code,
                "selected_profile": result["selected_profile"],
                "reasons": result["reason_codes"],
                "candidate_reasons": [item["reason_codes"] for item in result["candidates"]],
            }
assert results["original"]["selected_profile"] == {"id": "fixture-profile", "revision": 1}
assert results["actual_javascript_roundtrip"]["selected_profile"] is None
sources = [
    "frontend/src/RoutingSimulation.tsx", "backend/karajan/web/simulation.py",
    "backend/karajan/routing/evaluator.py", "backend/karajan/routing/models.py",
]
results["source_sha256"] = {
    path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in sources
}
output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8", newline="\n")
print(json.dumps(results, indent=2))
