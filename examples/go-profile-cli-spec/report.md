# Independent live-entry Spec review

Final result: **6 passed in 5.69 seconds**. One P2 finding, **GO-PROFILE-CLI-001**, is closed after independent replay of the original inputs. The reviewed entry is `examples/go-profile-qualification/run_live.py`, SHA256 `a613778a8f1b312f0f57896e37bcfe30ca8197a3672ee09e301e6f073b57b197`.

The original real-child-process test used a directory alias whose lexical path contained no credential, but whose resolved parent contained the synthetic credential string. The entry scanned only lexical arguments. ProjectRegistry subsequently persisted that string through the resolved repository path into public project/command metadata, before later setup rejected the directory. Original evidence is retained in `before.junit.xml` (1 failed, 5 passed) and `run_live.before.py.txt`.

The fix checks the lexical and resolved diagnostic directory plus the actual runtime binary path before any new directory or project database is created, then uses the selected physical root. The original input now leaves no public project metadata containing the credential. This does not claim to lock controller-owned parent directories against hostile concurrent replacement.

The other actual CLI negatives continue passing: no arguments; no `--live` with absent paths; missing required live arguments; existing directory preservation before credential resolution; and literal credential in a new output path. Output contains no supplied synthetic credential values. Live-path negatives run inside a fresh Linux network namespace, use a synthetic key and the real fixed runtime source, and make no provider request. No actual user credential was read.

Read-only code review confirms the entry creates its own diagnostic repository and controller generation, calls the same public store, and exports the exact record/start, historical replay comparison, journal facts and routing guard. Fixed observations remain separate from generic Task permission, remote cancellation and budget qualification. This review does not establish real authentication, successful official Go inference or full runtime qualification.

The original six independent tests are unchanged between red and green. No product, entry-script or author-test edits were made by this reviewer. No additional resolved-runtime-path execution is claimed; the corresponding descriptor check was read in the fix.

```text
wsl.exe -d Ubuntu -- /bin/sh -c 'cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/go-profile-qualification && KARAJAN_OPENCODE_LINUX_BINARY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest .cache/go-profile-cli-spec/test_live_entry.py -q --tb=short -p no:cacheprovider --junitxml=.cache/go-profile-cli-spec/after.junit.xml'
```
