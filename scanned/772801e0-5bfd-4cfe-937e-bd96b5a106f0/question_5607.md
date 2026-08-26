# Q5607: compiled artifact cache poisoning by key collision — logic.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, two modules whose cache keys collide because the key omits a semantics-affecting field, with deeply nested loops and a maximal br_table target list, and additionally with irreducible control flow that produces an unmetered edge, reach `promise_batch_action_state_init_by_account_id` in `runtime/near-vm-runner/src/wasmtime_runner/logic.rs` and make one contract execute another's compiled artifact, breaking the invariant that the artifact cache key covers code hash, VM kind, VM config, and protocol version, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/wasmtime_runner/logic.rs` :: `promise_batch_action_state_init_by_account_id`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: two modules whose cache keys collide because the key omits a semantics-affecting field; with deeply nested loops and a maximal br_table target list; with irreducible control flow that produces an unmetered edge
- Exploit idea: make one contract execute another's compiled artifact
- Invariant to test: the artifact cache key covers code hash, VM kind, VM config, and protocol version
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test enumerating cache-key inputs against config fields
