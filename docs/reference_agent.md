# Reference Agent: Requirements and Architecture Decision

## Requirements

- R1: A packaged `romp-agent run --config ... --task ...` entry runs a native
  Chat Completions tool loop. A `status` entry inspects persistent effects and
  admitted requests without contacting a model.
- R2: Host JSON configuration selects the policy, persistent state directory,
  subject, model endpoint, model, orders, and optional file targets. Paths resolve
  relative to the configuration file, not the working directory.
- R3: Every executable proposal passes through the StateEffectExecutor. The
  accounted effects of an operation come from the v0.3 policy declaration, not
  from arguments the host or model supplies. The model receives tool aliases,
  never filesystem paths, credentials, amounts to modify, or a database handle.
  No shell or arbitrary-code tool is registered.
- R4: Host-defined operation IDs are stable across turns, tasks, and restarts.
  Order amounts and target identities cannot be supplied or changed by the model.
- R5: Requests and local payment effects survive tasks and process restarts.
  Held capacity is a per-state vector, so an operation with more than one
  declared effect dimension is accounted on all of them. Missing one database,
  changed host bindings, or invalid configuration fails closed. A new directory
  is a new scope, not a reset of old spending.
- R6: Tool outcomes return to the model. UNKNOWN remains uncertain; REJECT and
  ESCALATE invoke no tool. Limits and model/protocol errors are explicit results,
  not successful task completion.
- R7: Local JSONL traces capture tool calls, results, and final termination.
  Authentication headers and provider raw responses are not persisted.
- R8: Deterministic unit/system tests cover actual local effects, protected
  deletion, persistent retries, unknown outcomes, malformed calls, and limits.
  Scripted-model tests are not reported as real-model experiments.

## ADR: Independent Core, Embedded Reference Agent

Keep the v0.3 declaration and executor unchanged. A small host owns resource
mapping, credentials, persistence, and dispatch; a model loop proposes native
tool calls. The reference Agent embeds the engine as a library. It demonstrates
the complete workflow without implementing a general planning framework or an
OS sandbox.

Charge uses LocalPaymentLedger, a local idempotent demonstrator, not a bank.
Delete uses the existing POSIX descriptor-relative backend. File targets must
already exist; the Agent does not create disposable fixtures automatically.
Host mappings bind the entire configured catalog, subject, and policy to the
request database. Editing policies/catalogs after initialization requires a
migration outside this prototype.

The host must exclusively control mapped directories and protect configuration,
state, and credentials from modification. Model API credentials are read from a
named environment variable. The subject is a trusted local configuration value,
not authentication. HTTPS protects transport; this client does not attest the
provider or isolate arbitrary agent code.

The trace is observational, not the authoritative accounting store. SQLite request
records and the separate payment ledger supply persistent accounting. Traces
contain tasks and model answers, so operators must treat them as potentially
private. UNKNOWN reconciliation and conversation continuation after a process
exit are not implemented. A later task can query/retry the same business ID.

## Acceptance Mapping

| Requirement | Acceptance evidence |
| --- | --- |
| R1, R6 | Scripted native-tool loop, CLI status and error tests |
| R2, R3 | Configuration/schema validation and out-of-catalog rejection |
| R4, R5 | Cross-instance retries, shared budget and second-dimension limits, missing DB/binding rejection |
| R3, R8 | Real POSIX deletion and protected target preservation |
| R6 | After-effect timeout, malformed response, turn/call limits |
| R7 | Trace inspection and safe API error handling |

## Usage

Install with `python -m pip install -e .`. Copy/edit the configuration example
in `examples/reference_agent.json` before initializing a scope. That example
selects `examples/policy_v03.json`, whose `states`, `operations.effects` and
`invariants` decide what the registered tools may account for. Set
`ROMP_API_MODEL_KEY` in the host environment; the config contains no secret.
The example endpoint/model are explicit configuration choices, not an assumption
about the provider available to every user.

```bash
romp-agent run --config examples/reference_agent.json \
  --task "Process invoice-001, then invoice-002. Report denied operations."
romp-agent status --config examples/reference_agent.json
```

The payment-only example runs on Windows and POSIX. To register deletion, add
existing, nonoverlapping roots and explicit targets, for example:

```json
{
  "roots": {"workspace": "./workspace", "system": "./system"},
  "protected_roots": ["system"],
  "targets": {
    "draft": {"id": "delete-draft-001", "root": "workspace", "path": "draft.txt"},
    "core": {"id": "delete-core-001", "root": "system", "path": "core.txt"}
  }
}
```

These are top-level config fields. Paths are relative to the config file.
The state directory and config must stay outside all mapped file roots.
Delete is unavailable on Windows; configuring it fails rather than silently
falling back to an unsafe implementation.

Exit codes: 0 = model returned a final answer; 1 = turn/call limit;
2 = configuration, API, protocol, or host error. A final answer is not proof of
business success. Inspect tool results and `status` for actual effects.
