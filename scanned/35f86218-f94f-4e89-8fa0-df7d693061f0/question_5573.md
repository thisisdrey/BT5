# Q5573: compiled artifact cache poisoning by key collision — instrument_v3.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, two modules whose cache keys collide because the key omits a semantics-affecting field, with deeply nested loops and a maximal br_table target list, and additionally with irreducible control flow that produces an unmetered edge, reach `add_globals` in `runtime/near-vm-runner/src/prepare/instrument_v3.rs` and make one contract execute another's compiled artifact, breaking the invariant that the artifact cache key covers code hash, VM kind, VM config, and protocol version, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/prepare/instrument_v3.rs` :: `add_globals`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: two modules whose cache keys collide because the key omits a semantics-affecting field; with deeply nested loops and a maximal br_table target list; with irreducible control flow that produces an unmetered edge
- Exploit idea: make one contract execute another's compiled artifact
- Invariant to test: the artifact cache key covers code hash, VM kind, VM config, and protocol version
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test enumerating cache-key inputs against config fields
