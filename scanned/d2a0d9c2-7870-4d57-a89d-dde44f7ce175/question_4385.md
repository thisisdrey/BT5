# Q4385: compiled artifact cache poisoning by key collision — prepare_v2.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, two modules whose cache keys collide because the key omits a semantics-affecting field, with a module one structural unit past a preparation limit, and additionally with deeply nested loops and a maximal br_table target list, reach `size_of_function_activation` in `runtime/near-vm-runner/src/prepare/prepare_v2.rs` and make one contract execute another's compiled artifact, breaking the invariant that the artifact cache key covers code hash, VM kind, VM config, and protocol version, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/prepare/prepare_v2.rs` :: `size_of_function_activation`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: two modules whose cache keys collide because the key omits a semantics-affecting field; with a module one structural unit past a preparation limit; with deeply nested loops and a maximal br_table target list
- Exploit idea: make one contract execute another's compiled artifact
- Invariant to test: the artifact cache key covers code hash, VM kind, VM config, and protocol version
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test enumerating cache-key inputs against config fields
