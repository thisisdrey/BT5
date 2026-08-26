# Q1888: logs and return-data size limits — types.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, log output and return data sized exactly at the per-receipt totals, with the input length at exactly the host function's accepted maximum, reach `as_value` in `runtime/near-vm-runner/src/logic/types.rs` and exceed the accounted total so the produced outcome exceeds chunk limits, breaking the invariant that log and return-data totals are enforced cumulatively, not per call, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `runtime/near-vm-runner/src/logic/types.rs` :: `as_value`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: log output and return data sized exactly at the per-receipt totals; with the input length at exactly the host function's accepted maximum
- Exploit idea: exceed the accounted total so the produced outcome exceeds chunk limits
- Invariant to test: log and return-data totals are enforced cumulatively, not per call
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: unit test accumulating logs to the configured total
