# Planning output review evidence

This directory preserves the independent GPT-6 high reviewer artifacts for
candidate `447319708801927c0063700d1d294b6c4b2bebfe`.

The boundary script was run with:

```text
C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe C:\Users\Chooo\Playground\Karajan\.cache\reviewer-high-109\check_boundaries.py
```

That command describes the original review workspace. The copied
`check_boundaries.py.txt` is a raw forensic archive of the reviewer script,
retained byte-for-byte; it is not a repository test and is not expected to run
from an arbitrary checkout because it references the original `.cache`
layout. The original script is intentionally not rewritten to satisfy
repository formatting rules.

The recorded scope is the 56 cases in `result.json`, covering content and
amount preservation, byte/depth limits, strict types, identity fields,
duplicate keys, surrogate handling, and oversized integers. The preserved
artifact is the raw `result.json`; no output has been regenerated or edited.

The independent Standards review recorded 23 passes and 0 findings. Its raw
log is not included here; this README records only that result.

The parser candidate was evaluated from the fixed `dev` baseline and this
evidence does not establish real model qualification, admission, budget, or
owner approval.
