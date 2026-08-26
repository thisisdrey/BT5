# Q5852: stack depth and operand stack limits — prepare_v3.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, recursive WASM calls to exactly the configured stack limit with maximal frame size, with deeply nested loops and a maximal br_table target list, and additionally with irreducible control flow that produces an unmetered edge, reach `run` in `runtime/near-vm-runner/src/prepare/prepare_v3.rs` and exhaust the host stack rather than hitting the metered limit, breaking the invariant that guest stack usage is bounded by metered limits before any host stack risk, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `runtime/near-vm-runner/src/prepare/prepare_v3.rs` :: `run`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: recursive WASM calls to exactly the configured stack limit with maximal frame size; with deeply nested loops and a maximal br_table target list; with irreducible control flow that produces an unmetered edge
- Exploit idea: exhaust the host stack rather than hitting the metered limit
- Invariant to test: guest stack usage is bounded by metered limits before any host stack risk
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: unit test recursing to the configured limit with large frames
