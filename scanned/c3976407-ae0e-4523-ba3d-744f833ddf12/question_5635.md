# Q5635: stale cached artifact after a config change — instrument_v3.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, a contract deployed before and executed after a protocol-version parameter change, with deeply nested loops and a maximal br_table target list, and additionally with irreducible control flow that produces an unmetered edge, reach `call_gas_instrumentation` in `runtime/near-vm-runner/src/prepare/instrument_v3.rs` and keep executing an artifact instrumented under the old gas schedule, breaking the invariant that artifacts are invalidated whenever the gas schedule or VM config changes, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/prepare/instrument_v3.rs` :: `call_gas_instrumentation`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: a contract deployed before and executed after a protocol-version parameter change; with deeply nested loops and a maximal br_table target list; with irreducible control flow that produces an unmetered edge
- Exploit idea: keep executing an artifact instrumented under the old gas schedule
- Invariant to test: artifacts are invalidated whenever the gas schedule or VM config changes
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: test-loop test executing a cached contract across a version bump
