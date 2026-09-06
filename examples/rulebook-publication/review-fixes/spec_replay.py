"""Replay fixed synthetic migration and invalid-draft histories on fresh SQLite."""

import argparse
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from karajan.projects import ProjectRegistry

DIRECTORY = Path(__file__).resolve().parent
ROOT = DIRECTORY.parents[2]


def source_hashes() -> dict[str, str]:
    backend = ROOT / "backend/karajan"
    paths = [*backend.glob("projects/*.py"), *backend.glob("routing/*.py")]
    paths.extend(
        backend / name
        for name in (
            "contracts/probe.py",
            "contracts/credentials.py",
            "resources/broker.py",
            "capacity/models.py",
        )
    )
    return {
        path.relative_to(backend).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", choices=("legacy-effective-catalog", "invalid-budget-catalog"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Historical reports cannot be overwritten")
    directory = args.directory
    if directory is None:
        directory = Path(tempfile.mkdtemp(prefix="karajan-publication-spec-"))
    else:
        directory.mkdir(parents=True, exist_ok=False)
    input_path = DIRECTORY / f"spec-{args.case}.input.json"
    raw = input_path.read_bytes()
    document = json.loads(raw)
    if args.case == "legacy-effective-catalog":
        base = document
    else:
        base_path = DIRECTORY / document["base_fixture"]
        base_raw = base_path.read_bytes()
        assert hashlib.sha256(base_raw).hexdigest() == document["base_fixture_sha256"]
        base = json.loads(base_raw)
    hashes = source_hashes()
    database = directory / "legacy.sqlite"
    with sqlite3.connect(database) as db:
        # The three exact old-format tables are a controlled migration fixture.
        assert set(base["legacy_tables"]) == {"projects", "commands", "previews"}
        for name, table in base["legacy_tables"].items():
            db.execute(table["ddl"])
            for row in table["rows"]:
                db.execute(
                    "INSERT INTO " + name + " VALUES (" + ",".join("?" for _ in row) + ")", row
                )
    service = ProjectRegistry(database, [directory])
    project_id = base["project_id"]
    report: dict[str, Any] = {
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "scope": (
            "fresh real SQLite; controlled legacy fixture and public ProjectRegistry; "
            "no model or cash requests"
        ),
    }
    if args.case == "legacy-effective-catalog":
        actual = service.get_effective_resources(project_id)
        expected = base["expected_effective_catalog"]
        report.update(actual=actual, expected=expected)
        report["passed"] = all(
            actual[name] == expected[name] for name in ("resources", "approved_profile_refs")
        )
    else:
        current = service.get(project_id)
        before = service.get_effective_resources(project_id)
        assert before["resources"]["profiles"][0]["enabled"] is False
        preview = service.preview_configuration(
            project_id,
            document["configuration_draft"],
            command_key="budget-draft-preview",
            principal="owner",
        )
        saved = service.apply_configuration(
            project_id,
            preview["preview_id"],
            expected_revision=current["revision"],
            command_key="budget-draft-apply",
            principal="owner",
        )
        after = service.get_effective_resources(project_id)
        report.update(before_catalog=before, after_catalog=after, preview=preview, saved=saved)
        report["passed"] = before == after and saved["configuration"]["status"] == "draft"
    report["source_sha256"] = source_hashes()
    assert hashes == report["source_sha256"], "Source changed during replay"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"case": args.case, "passed": report["passed"], "input_sha256": report["input_sha256"]}
        )
    )


if __name__ == "__main__":
    main()
