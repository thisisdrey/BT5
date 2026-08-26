# Q0779: stale cached artifact after a config change — mod.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, a contract deployed before and executed after a protocol-version parameter change, with function, local, table, and nesting limits all at their exact maxima, reach the primary handler in this file in `runtime/near-vm-runner/src/wasmtime_runner/mod.rs` and keep executing an artifact instrumented under the old gas schedule, breaking the invariant that artifacts are invalidated whenever the gas schedule or VM config changes, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/wasmtime_runner/mod.rs` :: primary handler
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: a contract deployed before and executed after a protocol-version parameter change; with function, local, table, and nesting limits all at their exact maxima
- Exploit idea: keep executing an artifact instrumented under the old gas schedule
- Invariant to test: artifacts are invalidated whenever the gas schedule or VM config changes
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: test-loop test executing a cached contract across a version bump
