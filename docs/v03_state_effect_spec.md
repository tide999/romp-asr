# ROMP-mu++/0.3: State, Effect Bounds, and Admission Constraints

## Requirements and Scope

R1. Authors declare named persistent gross counters in `states`, rather than
embedding candidate-state arithmetic in a business adapter.
R2. Each registered operation declares an `effects` vector: a nonnegative
integer constant or a trusted integer fact for each consumed counter.
R3. Hard `invariants` bound the candidate counter values. All invariants and
ordinary hard rules must pass before the existing soft decision can allow.
R4. Reading held state, evaluating rules/effects/invariants, and persisting
all reservations is one transaction. Commit precedes tool invocation.
R5. PENDING, SUCCEEDED, and UNKNOWN hold the complete vector. Only a trusted
no-effect failure during invocation releases it. Same-ID, same-payload retry
does not reserve again or invoke again. An existing UNKNOWN is not reconciled.
R6. v0.2 APIs, evidence, paper, and `submission/` remain unchanged. v0.3 is
an opt-in development implementation, not evidence from the v0.2 experiments.

## Language

The JSON/restricted-YAML document has `version`, `types`, `states`,
`operations`, `invariants`, `default`, `rules`, `order`, and `limits`.
Version is `romp-mu++/0.3`. Types/rules/order/limits retain v0.2 semantics.
The required string field `operation` is supplied by the executor, not by
the model or fact provider.

```json
{
  "states": {"spent": {"type": "gross"}, "charge_count": {"type": "gross"}},
  "operations": {
    "Charge": {
      "effects": {
        "spent": ["field", "amount_minor"],
        "charge_count": ["const", 1]
      }
    },
    "Delete": {"effects": {}}
  },
  "invariants": [
    {"id": "budget", "state": "spent", "max": 100},
    {"id": "charge_limit", "state": "charge_count", "max": 2}
  ]
}
```

These are declarations from a full policy, not a standalone policy.
`charge_count` counts charges that may have taken effect, not HTTP attempts.
Every declared state is releasable on a trusted no-effect result, because a
no-effect failure in that dimension means no effect occurred there. An attempt
or rate budget is therefore not expressible: attempts are not releasable, and a
backend that consumes a declared resource even on failure cannot raise
`NoEffectError` for that failure.
`field` references a declared integer fact. `const` is an integer in
0..2^63-1, excluding bool. Missing effects are zero. Every state must have
an invariant; several bounds on one state are conjunctive. Invariant IDs
are unique and distinct from rule IDs. Unknown keys/references are errors.
Limits: 16 states, 32 operations, 64 invariants, and the existing bounded
policy bytes, fields, rules, and expressions. No effect expressions other
than `field` and `const` are supported.

## Operational Semantics

For each state dimension j, R_j is the sum of reservations attached to
PENDING, SUCCEEDED, and UNKNOWN records. The compiled effect vector d is
computed from validated trusted facts. Candidate C_j = R_j + d_j must fit
signed 64-bit nonnegative arithmetic. All declared C_j <= max constraints
must hold, regardless of soft votes. Invalid facts/effects/state deny the
whole operation. Facts cannot supply or replace R_j.

The implementation initializes a *fresh* scope at zero. It does not import
historical effects or migrate an existing v0.2 scope. Use a distinct database;
never replace a consumed scope with a fresh one. Bindings cover the compiled
policy and declared adapter identities. State/rules cannot be hot-edited
in a bound scope. A policy change requires a separately designed migration.

Given covered initial history and a trustworthy effect-bound contract,
actual lifetime gross effects E_j obey 0 <= E_j <= R_j <= B_j. Delayed
effects count in E_j. A retry does not add an invocation; effects from an
original invocation may still arrive. External effects and result accounting
are not atomic. This is not exactly-once execution.

## Trusted Adapter Contract

`StateEffectAdapter(facts, invoke, binding)` supplies operation-specific
facts from validated parameters and host resources. It does not return an
amount, receive held state, or compute a candidate total. The runtime owns
those computations. Each invocation must, for every declared dimension,
produce nonnegative lifetime gross effects no greater than the evaluated
effect bound, including delayed effects. A bound is not a measured outcome.

The host must ensure the effect declaration matches the actual backend.
The engine cannot verify arbitrary physical side effects or discover undeclared
effects. Facts and invocation must refer to the same resource and arguments.
Providers must be read-only, bounded, and preserve the supplied arguments.
`NoEffectError` guarantees no present or delayed effect in *any* dimension.
Other exceptions retain every reservation. Host permissions, stable business
IDs, trusted backend bindings, and exclusive file-namespace control remain
required. SQLite DELETE/FULL and the process-exit fault scope are retained.

## ADR-003: Conservative Monotone State First

Chosen: named vector reservations, constant/trusted-field bounds, conjunctive
upper-bound invariants, reusable v0.2 pure rule evaluator, and a separate v0.3
executor/database. This puts state/effect relationships in the language while
preserving the proven old interfaces.

Rejected for this increment: arbitrary state assignment, decrement/refund,
non-monotone predicates, and general temporal operators. Conservatively
overestimating state preserves an upper-bound invariant, but need not preserve
an equality or a lower-bound precondition. These require different semantics.
Resource scoping is currently database-wide, not per subject/day/currency.

Contribution hypothesis: a language-level effect-bound admission contract
connects author constraints to runtime reservations without business-specific
candidate arithmetic. This is a development hypothesis; no novel transaction
algorithm or literature priority is established by this implementation.

## Verification and Tasks

- Compiler: references, integer types, unsafe expressions, invariant coverage,
  unknown keys, canonical declaration order, and bounded sizes.
- Pure admission: candidate vectors, invalid input, hard-rule precedence.
- Runtime: two independent counters, all-or-nothing admission, distinct-ID
  races, same-ID races, UNKNOWN retention, no-effect release, restart,
  binding conflicts, and rejection of existing v0.2 databases.
- System: a runnable local payment/file example exercises actual side effects.
- Regression: existing v0.2 tests run unchanged. No hosted-model results or
  submission files are relabeled as v0.3 evidence.

## Run the Development Example

From the implementation root, with Python 3.11 or later:

```sh
python examples/policy_v03_demo.py --state /tmp/romp-v03-demo --id a --amount 60
python examples/policy_v03_demo.py --state /tmp/romp-v03-demo --id b --amount 50
python examples/policy_v03_demo.py --state /tmp/romp-v03-demo --id a --amount 60
python examples/policy_v03_demo.py --state /tmp/romp-v03-demo
python -m pytest tests/test_policy_v03.py -q -p no:cacheprovider
```

Small charges that the budget absorbs still hit the charge limit, so the
second dimension rejects independently:

```sh
python examples/policy_v03_demo.py --state /tmp/romp-v03-limit --id a --amount 10
python examples/policy_v03_demo.py --state /tmp/romp-v03-limit --id b --amount 10
python examples/policy_v03_demo.py --state /tmp/romp-v03-limit --id c --amount 10
```

`c` is rejected by `charge_limit` while only 30 of the 100 budget is spent.
An uncertain result keeps its whole vector and blocks a new operation:

```sh
python examples/policy_v03_demo.py --state /tmp/romp-v03-unknown --id a --amount 60 --outcome unknown
python examples/policy_v03_demo.py --state /tmp/romp-v03-unknown --id b --amount 50
python examples/policy_v03_demo.py --state /tmp/romp-v03-unknown --id a --amount 60
```

`a` returns UNKNOWN after recording the effect; `b` is rejected by `budget`;
retrying `a` returns the stored UNKNOWN without a second invocation, and the
payment total stays 60.

Use a new, host-controlled state location for this new scope. Expected:
60 succeeds; 50 is rejected by `budget`; retry returns stored success;
payment total remains 60 and held state remains `{spent: 60, charge_count: 1}`.
On Windows supply an appropriate local state path.

The compiler entry is `compile_state_policy`; the pure decision entry is
`admit`; execution uses `StateEffectExecutor`. Public v0.2 defaults and the
existing `romp-agent` model loop have not switched to this version.
This local example is not a new hosted-model acceptance run.

## Executed Verification

On 2026-10-15, Linux/WSL passed all 144 tests: the existing 106 plus 38
state-effect tests. Windows passed 37 of the new tests and skipped the
POSIX deletion test. The new tests include actual payment records and
temporary-file deletion, vector retention after an after-effect process exit,
same-ID and distinct-ID concurrency, changed effect declarations without
adapter changes, and four CLI subprocesses sharing a persistent scope.
They do not establish arbitrary crash recovery, storage-corruption tolerance,
or compliance of third-party backends with declared effect bounds.

The current manuscript, submission snapshot, and hosted-model evidence remain
v0.2 artifacts. Before writing v0.3 results into the paper, the configurable
reference-agent integration and hosted-model acceptance must be updated and
verified explicitly. No new external model calls were made in this increment.
