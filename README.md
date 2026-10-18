# ROMP: A Safety Policy DSL and Controlled Tool Execution

Current version: `romp-mu++/0.3`. Requires Python 3.11 or later.
The JSON path has no runtime dependencies; YAML is an optional serialization
format. H-DSL and WASM are not implemented.

## Docker: No-Build Reviewer Access

Reviewers can use the image published on GHCR without building from source.

```bash
docker pull ghcr.io/tide999/romp-asr:0.3.0
docker run --rm ghcr.io/tide999/romp-asr:0.3.0              # Usage
docker run --rm ghcr.io/tide999/romp-asr:0.3.0 pytest -q    # 144 checks
docker run --rm ghcr.io/tide999/romp-asr:0.3.0 policy-demo
docker run --rm ghcr.io/tide999/romp-asr:0.3.0 agent-demo
```

Both demos run offline without an API key, using a local payment ledger and
temporary files. Expected results include budget-overrun rejection,
multi-dimensional effect reservations, retention of capacity on UNKNOWN,
and, in `agent-demo`, deletion of ordinary files while protected files remain.
Windows users can run these commands in WSL with a working Docker service.

The image uses `python:3.12-slim-bookworm`, `pytest==9.1.1`, and `PyYAML==6.0.3`,
matching the WSL environment described in the paper. Container validation
reported 144 passed. Dependency versions can be changed in `Dockerfile`;
update the paper's environment description if they change.

Linux containers support the POSIX `dir_fd` unlink operation required by the
file backend, so Windows reviewers can also run the deletion scenarios that
are skipped when running the sources directly on Windows.

Credentials are excluded from the image: `.dockerignore` explicitly excludes
`.env`, since Git ignore rules do not protect the Docker build context.
The acceptance `state/` scope and generated fixture directories are also
excluded, so consumed budget state is not packaged.

Live-model acceptance (Section 4.3) requires the reviewer's own API key and
consumes hosted-model quota:

```bash
mkdir -p out && cp .env out/
docker run --rm -v "$PWD/out:/work" ghcr.io/tide999/romp-asr:0.3.0 acceptance
```

`accept.py` is a one-shot harness that refuses to run when evidence or a scope
already exists. The entrypoint copies the harness into a writable directory
and clears its copied evidence files; outputs are written to `out/`.
Preserve failed samples.

### Maintainer Build and Publication

These commands are for maintainers, run from the source repository root in
Bash or WSL. Reviewers should use the no-build commands above:

```bash
docker build --platform linux/amd64 -t ghcr.io/tide999/romp-asr:0.3.0 .
docker login ghcr.io -u tide999
docker push ghcr.io/tide999/romp-asr:0.3.0
```

Authenticate with a GitHub classic token with `write:packages` permission,
not your account password. Do not overwrite a version tag used for submission;
use a new tag for subsequent changes.
A successful push does not make the image public: set its visibility to Public
under GitHub's Packages / romp-asr / Package settings, then verify pulling
and running it from an unauthenticated environment.

## Installation and Demonstration

```bash
python -m pip install -e .
python -m pip install ".[dev,yaml]"
python -m romp_asr --version
PYTHONPATH=src python examples/policy_v03_walkthrough.py
```

In Windows PowerShell, set `$env:PYTHONPATH="src"` before running the example.
The charge backend uses a persistent local payment ledger. Actual temporary-file
deletion is demonstrated only on POSIX systems.

## Compilation and Pure Evaluation

```bash
python -m romp_asr check examples/policy_v02.json
python -m romp_asr eval examples/policy_v02.json context.json
```

`context.json` contains `operation`, `amount_minor`, `candidate_total_minor`,
and `target_protected`, and must be constructed by the trusted host.
The CLI compiles and evaluates policies; it does not execute tools.
Exit codes: 0 for successful compilation or ALLOW; 1 for REJECT/ESCALATE;
2 for argument, compilation, or input-file errors.
The `decision` field in the JSON output is the policy decision; process success
alone does not establish that a business operation occurred.

Public APIs:
- v0.2 rule layer: `compile_policy`, `evaluate`, `PolicyExecutor`,
  `OperationAdapter`, `charge_adapter`, and `delete_adapter`.
- v0.3 effect layer: `compile_state_policy`, `admit`, `StateEffectExecutor`,
  and `StateEffectAdapter`. Adapters are defined by the host or examples.

Soft weights cannot override a hard rejection. An invalid context is rejected
as a whole before rule evaluation. Admission transactions reserve cumulative
capacity; retries do not invoke the tool again, and UNKNOWN/PENDING retain
their reservations.

Only admitted identifiers are persistently bound to arguments.
REJECT/ESCALATE do not claim an identifier and can be evaluated again.
A same-ID retry of FAILED returns the stored failure; a new attempt needs a
new identifier.
Edit rules before initializing the database. Changes to a bound policy require
explicit migration; migration and unknown-outcome reconciliation APIs are not
currently provided. A new database is a new budget scope, not a replacement
that restores already consumed capacity.
The host must ensure that the lifetime gross charges of an invocation do not
exceed its reservation.

## Tests and Boundaries

```bash
python -m pytest tests -q
```

Current test results: WSL 144 passed; Windows 127 passed with 17 POSIX tests
skipped. In the v0.3 charge walkthrough, scope A reports ledger totals of
60, 60, 60, and 100; scope B rejects on `charge_count` alone; scope C retains
the complete effect vector on UNKNOWN. On POSIX, `system/core` is preserved
and `workspace/draft` is deleted.
SQLite requires DELETE journal mode and `synchronous=FULL`. Tests cover selected
process-exit windows; they do not establish recovery from power loss or storage
corruption.

The trusted host is responsible for authentication, trusted facts, database
custody, and isolation of tool permissions. The file backend requires exclusive
host control of the directory namespace; target identity is not guaranteed
under malicious concurrent renames.
ROMP is not a banking connector, an OS sandbox, or a proof of complete business
correctness.

## Reference Agent

After installation, use `romp-agent`, rather than a fixed experiment-episode
entrypoint:

```bash
romp-agent run --config examples/reference_agent.json \
  --task "Process invoice-001, then invoice-002. Report denied operations."
romp-agent status --config examples/reference_agent.json
```

Set `ROMP_API_MODEL_KEY` in the host environment and configure the model and
HTTPS Chat Completions endpoint for your provider. The configuration does not
store the key. Order amounts, stable business identifiers, policies, and state
directories are host-configured; the model can submit only registered resource
aliases. Optional file targets use existing directories, require POSIX, and do
not automatically create temporary fixtures.

Model proposals execute only after `StateEffectExecutor` admits them according
to the v0.3 effect declarations. Results are fed back to the model so it can
continue the task. Requests and the local ledger persist across tasks and
restarts; `status` reports ledger totals, the multi-dimensional `held` vector,
and admission decisions.
`agent-trace.jsonl` records tasks, tool feedback, and final answers and may
contain private content; it is not the transaction ledger.
A `completed` termination means the model returned a final answer, not that the
task succeeded. Arbitrary code execution, an OS sandbox, automatic UNKNOWN
reconciliation, and production banking integration are not provided.

See `docs/reference_agent.md` for configuration, requirements, architecture
decisions, and acceptance mappings. Reference-agent tests use a scripted model
and are reported separately from live-model experiments.

## Migration

The legacy Runtime/ActionProposal model, dedicated `controlled-tools/1`
entrypoint, bench/eval aggregation/verify/perf CLI, and LLM/baseline experiment
pipeline are no longer part of the public execution path.
Historical sources, tests, experiment records, and fingerprints remain in the
development workspace; the standalone public source package excludes that
archive. Do not use an old database directly as a new one or switch versions to
clear capacity that is already held.
Historical contracts do not describe the current version, and compatibility
with legacy commands is not promised.

Current effect-accounting specification: `docs/v03_state_effect_spec.md`.
Rule-layer migration notes: `docs/v02_migration.md`.
