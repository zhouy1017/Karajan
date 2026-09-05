"""Rulebook persistence helpers; ProjectRegistry owns every transaction and command."""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import ConfigurationDraft


class PublicationError(ValueError):
    pass


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def compiler_identity() -> str:
    root = Path(__file__).parents[1]
    sources = (
        "routing/compiler.py",
        "routing/models.py",
        "contracts/probe.py",
        "projects/models.py",
        "projects/configuration.py",
        "projects/publication.py",
    )
    return digest(
        {source: hashlib.sha256((root / source).read_bytes()).hexdigest() for source in sources}
    )


def compile_document(document: Any) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    # Lazy import avoids routing.models -> projects.__init__ -> registry recursion.
    from karajan.routing import RoutingError, compile_rulebook

    if not isinstance(document, dict):
        return None, [{"path": "rulebook", "code": "RULEBOOK_REQUIRED"}]
    try:
        compiled = compile_rulebook(document)
        return compiled, compiled["issues"]
    except RoutingError as error:
        return None, [{"path": "rulebook", "code": error.code}]


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS project_owners "
        "(project_id TEXT PRIMARY KEY, principal TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS rulebook_versions (project_id TEXT NOT NULL, "
        "id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL, "
        "result TEXT NOT NULL, PRIMARY KEY(project_id,id,revision))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS rulebook_publications "
        "(sequence INTEGER PRIMARY KEY, project_id TEXT NOT NULL, result TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS effective_catalogs "
        "(project_id TEXT PRIMARY KEY, result TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS rulebook_conflicts (project_id TEXT NOT NULL, "
        "id TEXT NOT NULL, revision INTEGER NOT NULL, digests TEXT NOT NULL, "
        "PRIMARY KEY(project_id,id,revision))"
    )
    db.execute("CREATE TABLE IF NOT EXISTS publication_migrations (version INTEGER PRIMARY KEY)")
    if db.execute("SELECT 1 FROM publication_migrations WHERE version=1").fetchone() is None:
        migrate_legacy(db)
        db.execute("INSERT INTO publication_migrations VALUES (1)")


def migrate_legacy(db: sqlite3.Connection) -> None:
    projects = {
        row["id"]: json.loads(row["snapshot"])
        for row in db.execute("SELECT id,snapshot FROM projects")
    }
    owners: dict[str, set[str]] = {}
    applied: set[tuple[str, int, str]] = set()
    for row in db.execute("SELECT principal,result FROM commands ORDER BY rowid"):
        result = json.loads(row["result"])
        if result.get("schema_version") != "karajan.project.v1" or result.get("id") not in projects:
            continue
        project_id = result["id"]
        if result.get("revision") == 1:
            owners.setdefault(project_id, set()).add(row["principal"])
        preview_id = result.get("configuration", {}).get("preview_id")
        if preview_id is not None:
            applied.add((project_id, result["revision"], preview_id))
    for project_id, people in owners.items():
        if len(people) == 1:
            db.execute(
                "INSERT OR IGNORE INTO project_owners VALUES (?,?)",
                (project_id, next(iter(people))),
            )
    for project_id, snapshot in projects.items():
        preview_id = snapshot.get("configuration", {}).get("preview_id")
        if preview_id is not None:
            applied.add((project_id, snapshot["revision"], preview_id))
    # Replay accepted configuration history in project revision order. A later
    # invalid draft must not erase the last valid catalog or a recorded revocation.
    for project_id, _revision, preview_id in sorted(applied):
        row = db.execute(
            "SELECT configuration FROM previews WHERE project_id=? AND id=?",
            (project_id, preview_id),
        ).fetchone()
        if row is not None and row["configuration"] is not None:
            apply_catalog(db, project_id, json.loads(row["configuration"]))
    histories: dict[tuple[str, str, int], dict[str, tuple[dict[str, Any], dict[str, Any]]]] = {}
    for project_id, preview_id in sorted(
        {(project_id, preview_id) for project_id, _, preview_id in applied}
    ):
        row = db.execute(
            "SELECT configuration FROM previews WHERE project_id=? AND id=?",
            (project_id, preview_id),
        ).fetchone()
        if row is None or row["configuration"] is None:
            continue
        document = json.loads(row["configuration"]).get("rulebook")
        compiled, issues = compile_document(document)
        if compiled is None or issues:
            continue
        key = (project_id, document["id"], document["revision"])
        histories.setdefault(key, {})[compiled["rulebook_sha256"]] = (document, compiled)
    for key, versions in histories.items():
        if len(versions) > 1:
            db.execute(
                "INSERT INTO rulebook_conflicts VALUES (?,?,?,?)", (*key, encoded(sorted(versions)))
            )
        else:
            document, compiled = next(iter(versions.values()))
            bind_version(db, key[0], document, compiled)


def guard_identity(
    db: sqlite3.Connection, project_id: str, document: Any, compiled: dict[str, Any] | None
) -> None:
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("id"), str)
        or type(document.get("revision")) is not int
    ):
        return
    key = (project_id, document["id"], document["revision"])
    if db.execute(
        "SELECT 1 FROM rulebook_conflicts WHERE project_id=? AND id=? AND revision=?", key
    ).fetchone():
        raise PublicationError("LEGACY_RULEBOOK_IDENTITY_CONFLICT")
    existing = db.execute(
        "SELECT digest FROM rulebook_versions WHERE project_id=? AND id=? AND revision=?", key
    ).fetchone()
    if existing is not None and (
        compiled is None or compiled["rulebook_sha256"] != existing["digest"]
    ):
        raise PublicationError("RULEBOOK_REVISION_CONFLICT")


def bind_version(
    db: sqlite3.Connection, project_id: str, document: dict[str, Any], compiled: dict[str, Any]
) -> dict[str, Any]:
    guard_identity(db, project_id, document, compiled)
    key = (project_id, document["id"], document["revision"])
    existing = db.execute(
        "SELECT result FROM rulebook_versions WHERE project_id=? AND id=? AND revision=?", key
    ).fetchone()
    if existing is not None:
        return dict(json.loads(existing["result"]))
    result = {
        "project_id": project_id,
        "id": document["id"],
        "revision": document["revision"],
        "rulebook_sha256": compiled["rulebook_sha256"],
        "compiler_revision": compiled["compiler_revision"],
        "compiler_identity": compiler_identity(),
        "executable": {
            key: value
            for key, value in compiled["document"].items()
            if key not in {"id", "revision", "description", "status"}
        },
        "activation_allowed": False,
    }
    db.execute(
        "INSERT INTO rulebook_versions VALUES (?,?,?,?,?)",
        (*key, compiled["rulebook_sha256"], encoded(result)),
    )
    return result


def effective_catalog(db: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT result FROM effective_catalogs WHERE project_id=?", (project_id,)
    ).fetchone()
    return (
        dict(json.loads(row["result"]))
        if row
        else {
            "project_id": project_id,
            "revision": 0,
            "digest": None,
            "resources": None,
            "approved_profile_refs": [],
        }
    )


def apply_catalog(db: sqlite3.Connection, project_id: str, configuration: dict[str, Any]) -> None:
    from karajan.contracts.credentials import contains_credential

    from .configuration import resource_catalog_issues

    if contains_credential(configuration):
        return
    try:
        json.dumps(configuration, ensure_ascii=False, allow_nan=False).encode("utf-8")
        parsed = ConfigurationDraft.model_validate(configuration).model_dump()
    except (ValueError, TypeError):
        return
    resources = parsed["resources"]
    if resources is None or resource_catalog_issues(parsed):
        return
    previous = effective_catalog(db, project_id)
    content = {"resources": resources, "approved_profile_refs": parsed["approved_profile_refs"]}
    hashed = digest(content)
    if previous["digest"] == hashed:
        return
    result = {
        "project_id": project_id,
        "revision": previous["revision"] + 1,
        "digest": hashed,
        **content,
    }
    db.execute(
        "INSERT INTO effective_catalogs VALUES (?,?) "
        "ON CONFLICT(project_id) DO UPDATE SET result=excluded.result",
        (project_id, encoded(result)),
    )
