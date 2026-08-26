# Q0583: compiled artifact cache poisoning by key collision — logic.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, two modules whose cache keys collide because the key omits a semantics-affecting field, with function, local, table, and nesting limits all at their exact maxima, reach `promise_batch_action_add_gas_key_with_function_call` in `runtime/near-vm-runner/src/wasmtime_runner/logic.rs` and make one contract execute another's compiled artifact, breaking the invariant that the artifact cache key covers code hash, VM kind, VM config, and protocol version, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/wasmtime_runner/logic.rs` :: `promise_batch_action_add_gas_key_with_function_call`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: two modules whose cache keys collide because the key omits a semantics-affecting field; with function, local, table, and nesting limits all at their exact maxima
- Exploit idea: make one contract execute another's compiled artifact
- Invariant to test: the artifact cache key covers code hash, VM kind, VM config, and protocol version
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test enumerating cache-key inputs against config fields
