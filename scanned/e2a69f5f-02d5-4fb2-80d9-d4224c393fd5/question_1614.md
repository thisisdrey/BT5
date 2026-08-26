# Q1614: function-call cost vs signature complexity — profile.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, exported functions with maximal parameter counts and types, with function, local, table, and nesting limits all at their exact maxima, reach `get_action_cost` in `runtime/near-vm-runner/src/profile.rs` and make per-call overhead exceed what the call fee prices, breaking the invariant that call fees bound the real per-call overhead for any accepted signature, leading to High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)?

## Target
- File/function: `runtime/near-vm-runner/src/profile.rs` :: `get_action_cost`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: exported functions with maximal parameter counts and types; with function, local, table, and nesting limits all at their exact maxima
- Exploit idea: make per-call overhead exceed what the call fee prices
- Invariant to test: call fees bound the real per-call overhead for any accepted signature
- Expected Immunefi impact: High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)
- Fast validation: estimator comparing call overhead across signature shapes
