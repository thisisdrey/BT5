# Q1258: outgoing metadata group boundaries — congestion_control.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a stream of receipts sized to create a maximal number of metadata groups, when receipt sizes sit exactly on the bandwidth-request granularity boundary, reach `finalize_stats_get_outgoing_receipts` in `runtime/runtime/src/congestion_control.rs` and make metadata group bookkeeping lose or double-count queued bytes, breaking the invariant that outgoing metadata always reflects the exact bytes and count in the buffer, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `runtime/runtime/src/congestion_control.rs` :: `finalize_stats_get_outgoing_receipts`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a stream of receipts sized to create a maximal number of metadata groups; when receipt sizes sit exactly on the bandwidth-request granularity boundary
- Exploit idea: make metadata group bookkeeping lose or double-count queued bytes
- Invariant to test: outgoing metadata always reflects the exact bytes and count in the buffer
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: store test asserting metadata equals a full buffer scan
