# Python quality job budget

The Python quality matrix keeps the same tests, independent examples, locked
dependencies, tokenizer requirement, isolation requirement, and required
quality gate on both operating systems. The Linux job retains a 40-minute
limit. The Windows job has a 60-minute limit because the fixed push-run
evidence reached 93% of the 2,704-item pytest suite at about 38 minutes and
was then cancelled by the prior 40-minute job limit.

The cancelled run had already passed tokenizer preparation, Ruff, and mypy;
its cancellation stack location is not treated as evidence of a product
deadlock. A separate Windows run of the same full gate completed in about 29
minutes, so this is a bounded capacity adjustment for observed host variance.
All later checks remain in the workflow and still fail the job when they fail
or when the revised budget is exceeded.
