# Business planning demo setup

This directory prepares the first #153 workbench demo input and, only with an explicit `--live`, runs one independent Commander qualification through the public #147 producer. It does not create a Plan, admit a run, approve a version, or call the web factory.

Without `--live`, the command checks the fixed runtime and tokenizer paths, confirms that the state directory is new, and reports the paths it would use. It never opens the credential file or contacts a provider.

From WSL, after choosing a new private state directory under `/tmp`:

```bash
cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/business-planning-153-20260908
PYTHONPATH=backend python examples/business-planning-demo/run_live.py \
  --runtime /mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode \
  --tokenizer-directory /mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts \
  --credential-file /mnt/c/Users/Chooo/Playground/Karajan/opencodego.key.txt \
  --directory /tmp/karajan-business-planning-demo-20260908 \
  --live
```

The live command creates a private state directory and a seed Git repository containing the fixed `greeting.py` seed (`return f"Hello, {name}!"`). The later business flow retains the empty-name `Guest` and non-empty `Ada` requirements; this setup command does not implement that product change. It provisions an empty protected planning bootstrap (projects, runs, capacity, planning execution and admission stores) and writes both protected descriptors in the same control directory. The Commander project/profile/source are then registered in the bootstrap's projects database, so the public qualification store and later planning factory reopen one state. The command registers the supplied existing Go credential through `CredentialSourceStore`/`LocalKeyFile`, opens #147's public store, and calls the bounded `legal_plan`/`denied_tool` qualification once. The bootstrap creates no Run, Plan, admission or approval. The output contains only the `secret_ref`, provider/profile identifiers, current qualification status, redacted source metadata, and public state paths. It never prints key contents.

Qualification failures and unknown outcomes return the public status while preserving the protected start and journal in the dedicated state directory. The command does not retry with another key or claim business readiness. The later web planning factory consumes the public state; it owns Plan generation and approval.
