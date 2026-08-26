# Q1172: stack depth and operand stack limits — profile.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, recursive WASM calls to exactly the configured stack limit with maximal frame size, with function, local, table, and nesting limits all at their exact maxima, reach `test_with_config` in `runtime/near-vm-runner/src/profile.rs` and exhaust the host stack rather than hitting the metered limit, breaking the invariant that guest stack usage is bounded by metered limits before any host stack risk, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `runtime/near-vm-runner/src/profile.rs` :: `test_with_config`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: recursive WASM calls to exactly the configured stack limit with maximal frame size; with function, local, table, and nesting limits all at their exact maxima
- Exploit idea: exhaust the host stack rather than hitting the metered limit
- Invariant to test: guest stack usage is bounded by metered limits before any host stack risk
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: unit test recursing to the configured limit with large frames
