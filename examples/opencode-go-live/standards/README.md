# Independent Standards evidence

[Review](standards-review.md) and [exact source/evidence bindings](standards-review.json): no open findings; GO-STANDARDS-001 closed on its original inputs.

`independent_cases.py` contains eight offline cases, including five originally failing credential echoes. The two `before` JUnit files preserve the real failures; `standards-final.junit.xml` is the successful run from this published path after formatting. Formatting preserved the Python AST. No real credential or provider call is used by these cases.
