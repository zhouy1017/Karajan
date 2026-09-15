"""Expose the scheduling fixtures to every case in this directory.

The fixtures live in ``scheduling_fixtures`` beside these cases, and pytest puts
this directory on ``sys.path`` for them. Importing them *here* rather than in
each module is what keeps a fixture from colliding with a test's own parameter of
the same name: ``case`` is a fixture, and a test that also names a local ``case``
would otherwise be redefining the import.
"""

from scheduling_fixtures import (  # noqa: F401
    case,
    granted_case,
    run_case,
)
