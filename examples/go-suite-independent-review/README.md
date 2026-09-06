# Independent fixed Go suite review

The public test file is the exact byte copy of the final independently executed
11-case Linux suite. Read `review.md` for the closed grant-ownership finding and
the remaining limit: three early failures were not individually diagnosed.
A real WSL clock regression was measured and separately replayed through the public
suite clock boundary. No production time or authorization rule was relaxed.

Run from the repository root in Linux/WSL using the prepared fixed OpenCode 1.18.29
artifact. The helper import is intentional: it supplies only synthetic start,
credential and HTTP response DTOs from `tests/projects/test_go_suite.py`.

```sh
PYTHONPATH="$PWD/backend:$PWD/tests/projects" \
KARAJAN_OPENCODE_LINUX_BINARY=/path/to/pinned/opencode \
python -m pytest examples/go-suite-independent-review/test_go_suite_review.py \
  -q -p no:cacheprovider -o 'pythonpath=backend tests/projects' \
  --basetemp=.cache/go-suite-independent-review/replay-tmp
```

The ELF digest must be
`ca6c0e1f42be3120595bf6848937e7586ec862c87fa7aa111e89c7cc6e9a4650`.
The tests use real native Linux namespaces, Unix sockets and SQLite with a
synthetic HTTP upstream; they never call the real provider or read its key.
Actual Unix sockets and transient state use short native `/tmp` paths and are
removed automatically. Windows collects the file but skips all Linux cases.

`review.json` binds current source, test, final JUnit and all archived evidence.
Only this directory's `test_go_suite_review.py` is collected as a current test.
Historical Python files and the optional diagnostic plugin use `.py.txt` suffixes
so they cannot silently add tests or tracing to CI. The final 11 cases have no
verbose diagnostic plugin enabled.

`history/trace-1.xml` through `trace-3.xml` retain the bounded diagnostic rounds.
To reproduce that historical diagnostic configuration, copy
`history/test_go_suite_review.before-stable-clock.py.txt` as
`.cache/go-suite-independent-review/test_go_suite_review.py`, and copy
`history/trace_suite_exceptions.py.txt` into the same cache directory as
`trace_suite_exceptions.py`. Add that directory to PYTHONPATH and pass
`-p trace_suite_exceptions -o junit_logging=all` to pytest. This intentionally
replays the older host-clock test inputs and is not the current CI configuration.
Do not enable this diagnostic plugin for actual provider calls.

Some intermediate failures were captured before full observation logging existed;
their original XML is retained unchanged. Not every intermediate formatting/logging
variant was separately snapshotted, so those inputs are not claimed to have an
exact independently captured source digest. Initial, pre-stable-clock and final
input snapshots are retained explicitly. No SQLite database or temporary native
workspace is published.
