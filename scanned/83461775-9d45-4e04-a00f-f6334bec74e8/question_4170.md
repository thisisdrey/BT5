# Q4170: WASM preparation limits at their exact boundaries — runner.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, a module with function count, local count, table size, and nesting depth exactly at the configured limits, with a module one structural unit past a preparation limit, and additionally with deeply nested loops and a maximal br_table target list, reach `try_precompile` in `runtime/near-vm-runner/src/runner.rs` and get a module past preparation whose compiled form exceeds what the limits were meant to bound, breaking the invariant that every prepared module respects the declared structural limits after instrumentation, leading to High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)?

## Target
- File/function: `runtime/near-vm-runner/src/runner.rs` :: `try_precompile`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: a module with function count, local count, table size, and nesting depth exactly at the configured limits; with a module one structural unit past a preparation limit; with deeply nested loops and a maximal br_table target list
- Exploit idea: get a module past preparation whose compiled form exceeds what the limits were meant to bound
- Invariant to test: every prepared module respects the declared structural limits after instrumentation
- Expected Immunefi impact: High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)
- Fast validation: unit test compiling boundary modules and measuring artifact size and time
