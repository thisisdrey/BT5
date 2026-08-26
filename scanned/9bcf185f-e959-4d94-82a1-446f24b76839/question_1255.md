# Q1255: stack depth and operand stack limits — features.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, recursive WASM calls to exactly the configured stack limit with maximal frame size, with function, local, table, and nesting limits all at their exact maxima, reach `from` in `runtime/near-vm-runner/src/features.rs` and exhaust the host stack rather than hitting the metered limit, breaking the invariant that guest stack usage is bounded by metered limits before any host stack risk, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `runtime/near-vm-runner/src/features.rs` :: `from`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: recursive WASM calls to exactly the configured stack limit with maximal frame size; with function, local, table, and nesting limits all at their exact maxima
- Exploit idea: exhaust the host stack rather than hitting the metered limit
- Invariant to test: guest stack usage is bounded by metered limits before any host stack risk
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: unit test recursing to the configured limit with large frames
