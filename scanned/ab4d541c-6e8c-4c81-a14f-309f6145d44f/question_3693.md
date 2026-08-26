# Q3693: receipt buffering across a shard-layout change — congestion_control.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, buffered receipts targeting a shard id that ceases to exist at the resharding boundary, when receipt sizes sit exactly on the bandwidth-request granularity boundary, and additionally when the shard is driven exactly onto a congestion threshold, reach `action_receipt_congestion_gas` in `runtime/runtime/src/congestion_control.rs` and strand buffered value addressed to a retired shard, breaking the invariant that buffered receipts are remapped to the successor shard at layout changes, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `runtime/runtime/src/congestion_control.rs` :: `action_receipt_congestion_gas`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: buffered receipts targeting a shard id that ceases to exist at the resharding boundary; when receipt sizes sit exactly on the bandwidth-request granularity boundary; when the shard is driven exactly onto a congestion threshold
- Exploit idea: strand buffered value addressed to a retired shard
- Invariant to test: buffered receipts are remapped to the successor shard at layout changes
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: test-loop test with receipts buffered across a resharding boundary
