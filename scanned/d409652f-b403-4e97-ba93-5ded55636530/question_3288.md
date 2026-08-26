# Q3288: stack depth and operand stack limits — runner.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, recursive WASM calls to exactly the configured stack limit with maximal frame size, with function, local, table, and nesting limits all at their exact maxima, and additionally with a module one structural unit past a preparation limit, reach `run` in `runtime/near-vm-runner/src/runner.rs` and exhaust the host stack rather than hitting the metered limit, breaking the invariant that guest stack usage is bounded by metered limits before any host stack risk, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `runtime/near-vm-runner/src/runner.rs` :: `run`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: recursive WASM calls to exactly the configured stack limit with maximal frame size; with function, local, table, and nesting limits all at their exact maxima; with a module one structural unit past a preparation limit
- Exploit idea: exhaust the host stack rather than hitting the metered limit
- Invariant to test: guest stack usage is bounded by metered limits before any host stack risk
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: unit test recursing to the configured limit with large frames
