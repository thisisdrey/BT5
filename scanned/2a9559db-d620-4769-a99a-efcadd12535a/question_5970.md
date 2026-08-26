# Q5970: function-call cost vs signature complexity — runner.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, exported functions with maximal parameter counts and types, with deeply nested loops and a maximal br_table target list, and additionally with irreducible control flow that produces an unmetered edge, reach `precompile` in `runtime/near-vm-runner/src/runner.rs` and make per-call overhead exceed what the call fee prices, breaking the invariant that call fees bound the real per-call overhead for any accepted signature, leading to High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)?

## Target
- File/function: `runtime/near-vm-runner/src/runner.rs` :: `precompile`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: exported functions with maximal parameter counts and types; with deeply nested loops and a maximal br_table target list; with irreducible control flow that produces an unmetered edge
- Exploit idea: make per-call overhead exceed what the call fee prices
- Invariant to test: call fees bound the real per-call overhead for any accepted signature
- Expected Immunefi impact: High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)
- Fast validation: estimator comparing call overhead across signature shapes
