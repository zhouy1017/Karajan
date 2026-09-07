# #104 author evidence — pure review-content parser

Implementation base is `2e587d1773c514361689e13ebbd16ba62f1cd219`; implementation is not committed by this author. The parent owns publication, independent Standards/Spec and current-head CI checks. This package proves the stated C boundary, not Reviewer qualification, a production consumer, P/S/G completion or parent Issue #95/#13/#14 completion. No model, provider or credential was used.

Final public parser: `backend/karajan/candidates/review_output.py`; formal input: `tests/candidates/test_review_output.py`. Parser revision `karajan.review-output-parser.v1`, 269 cases. Final `tests/candidates` is 375 unique test IDs including those 269: Windows 372 passed / 3 POSIX permission skips; Linux 375 passed / 0 skipped. Do not sum the overlapping parser-only and Candidate runs. Old storage gate and fixture behavior are unchanged; the existing synthetic controller fixture plus real temporary Git/CAS only verifies content-to-storage compatibility.

## Red/green sequence and retained setup failure

- `initial-test.py.txt` + `initial-missing-entry.xml/.log`: actual missing public module, 1 collection error; `first-green.xml`: three mappings passed.
- `json-red-test.py.txt`, `json-red.xml/.log`: 34 failed / 4 passed; `json-green.xml/.log`: 38 passed.
- `fields-red-test.py.txt`, `fields-red.xml/.log`: 124 failed / 44 passed; `fields-green.xml/.log`: 168 passed.
- `scope-red-test.py.txt`, `scope-red.xml/.log`: 41 failed / 225 passed; `scope-green.xml/.log`: 266 passed.
- `parser-final-windows.xml/.log`: final 269 passed after additional already-supported precedence/UTF-8/public subtype cases and test formatting.
- `candidate-final-windows.xml/.log`: 372 passed / 3 skipped.
- `candidate-final-linux.xml/.log` retains the first Linux regression: 374 passed / 1 failed. The old child-process probe imports `karajan` outside pytest's injected path, and its inherited environment lacked PYTHONPATH. Traceback is ModuleNotFoundError; no parser failure or source fix occurred.
- `candidate-linux-path-corrected.xml/.log`: same 375 cases passed with explicit worktree backend PYTHONPATH. The original failed files were not overwritten.

Before the first Linux test command could execute, a sandbox WSL startup returned `Wsl/Service/CreateInstance/E_ACCESSDENIED`; no test report existed for that launcher attempt. The reviewed local WSL retry ran the above suite. The unrelated metadata read typo `Select-Object -First seventy` failed before a corrected read; no source/test input was changed by it.

## Reproduction

From this worktree, Windows used the absolute `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe`:

```text
python -m pytest tests/candidates/test_review_output.py --junitxml=.cache/review-parser-author/parser-final-windows.xml
python -m pytest tests/candidates --junitxml=.cache/review-parser-author/candidate-final-windows.xml
python -m ruff check .
python -m mypy backend
python -m ruff format --check backend/karajan/candidates/review_output.py tests/candidates/test_review_output.py
```

The actual Linux executable was `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`, with cwd `/mnt/c/Users/Chooo/Playground/Karajan/.cache/review-output-parser`:

```sh
PYTHONPATH="$PWD/backend" /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest -p no:cacheprovider --basetemp /tmp/karajan-review-parser-104-path tests/candidates --junitxml=.cache/review-parser-author/candidate-linux-path-corrected.xml
```

Static output files `ruff-final.log`, `mypy-final.log`, `format-final.log` and `static-exits.json` preserve actual commands' zero exits: full-repository Ruff; mypy backend 123 source files; both new Python inputs formatted. Earlier development lint findings were imports/line wrapping and fixed before the final run; they are not a remote CI result.

`final-sources.json` records final working bytes after the frozen-source runs; it is not mislabeled a pre-run snapshot. `freeze.json` binds the announced parser/test SHA, all reports, the unchanged existing storage contract against Git base blobs, and the final source map. The generated four owned source/doc files were independently checked for LF/no BOM/no trailing spaces. No staging or staged-whitespace verification was performed; historical Windows outputs retain original newline bytes. Copy only selected inputs/reports from this directory during publication, not build_evidence.py, pytest state, temporary Git repositories or databases.
