# Q2928: compiled artifact cache poisoning by key collision — instrument_v3.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, two modules whose cache keys collide because the key omits a semantics-affecting field, with function, local, table, and nesting limits all at their exact maxima, and additionally with a module one structural unit past a preparation limit, reach `checked_add_i64` in `runtime/near-vm-runner/src/prepare/instrument_v3.rs` and make one contract execute another's compiled artifact, breaking the invariant that the artifact cache key covers code hash, VM kind, VM config, and protocol version, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/prepare/instrument_v3.rs` :: `checked_add_i64`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: two modules whose cache keys collide because the key omits a semantics-affecting field; with function, local, table, and nesting limits all at their exact maxima; with a module one structural unit past a preparation limit
- Exploit idea: make one contract execute another's compiled artifact
- Invariant to test: the artifact cache key covers code hash, VM kind, VM config, and protocol version
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test enumerating cache-key inputs against config fields
