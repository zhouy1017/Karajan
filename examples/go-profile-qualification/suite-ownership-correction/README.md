# Grant ownership correction evidence

`freeze.json` is the current publication manifest. Its repository-relative paths resolve from the repository root. It binds the corrected suite source (`942b23a8…`) and author tests (`df4d6a90…`) to the exact captured reports: two red ownership cases, fourteen green boundary cases, eighteen final Linux passes, and eight Windows passes with ten Linux-only skips.

The correction checks the journal's full immutable binding before revocation. A colliding grant belonging to another start remains unchanged; a matching grant whose creation reply was lost is still revoked. The finding belongs to the independent reviewer; the two added author tests cover first/second scenario collisions changing authentication generation.

`author-freeze.original.json` is the byte-exact pre-publication author record. Its `.cache` paths and command are historical execution locations; `freeze.json` maps the selected artifacts to their published paths. Original main-directory `c7e7dedc…` reports remain unchanged. Independent review results are maintained separately.

Publication copied only the named JSON/XML evidence, verified copy and current source hashes, and recaptured the static checks in `static-checks.txt`. No native, model, or provider tests were rerun for publication. No databases, temporary native directories, bytecode, or user keys are included.
