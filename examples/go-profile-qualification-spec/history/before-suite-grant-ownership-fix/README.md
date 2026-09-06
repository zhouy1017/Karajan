# Historical input filename

The frozen `c7e7dedc…` suite review originally used `test_public_persistence.py`.
Its published historical copy is now [test_public_persistence.py.txt](test_public_persistence.py.txt)
so recursive pytest collection cannot import it instead of the current independent test.

Only the published filename changed. SHA256 remains
`5c9fc57b333d7df3065d60bc45b2f5afc3f3fe9f0b739fe932c7533229c78148`.
The original XML, JSON, report, and freeze metadata remain unchanged, including their original `.py`
filename references. The parent publication copy manifest maps that original cache path to this
`.py.txt` artifact. Execute only the [current test](../../test_public_persistence.py).
