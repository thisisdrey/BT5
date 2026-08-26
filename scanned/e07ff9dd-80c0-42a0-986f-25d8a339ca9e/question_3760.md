# Q3760: logs and return-data size limits — context.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, log output and return data sized exactly at the per-receipt totals, with the input length at exactly the host function's accepted maximum, and additionally with the input length one byte past the accepted maximum, reach `is_view` in `runtime/near-vm-runner/src/logic/context.rs` and exceed the accounted total so the produced outcome exceeds chunk limits, breaking the invariant that log and return-data totals are enforced cumulatively, not per call, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `runtime/near-vm-runner/src/logic/context.rs` :: `is_view`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: log output and return data sized exactly at the per-receipt totals; with the input length at exactly the host function's accepted maximum; with the input length one byte past the accepted maximum
- Exploit idea: exceed the accounted total so the produced outcome exceeds chunk limits
- Invariant to test: log and return-data totals are enforced cumulatively, not per call
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: unit test accumulating logs to the configured total
