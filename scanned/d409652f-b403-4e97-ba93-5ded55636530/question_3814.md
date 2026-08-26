# Q3814: congestion info for a missing chunk — receipt.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, traffic timed so the target shard's chunk is missing for several heights, when receipt sizes sit exactly on the bandwidth-request granularity boundary, and additionally when the shard is driven exactly onto a congestion threshold, reach `is_instant_receipt` in `core/primitives/src/receipt.rs` and have congestion info extrapolated for missing chunks in a way that differs between nodes, breaking the invariant that congestion info for missing chunks is derived deterministically from the last present chunk, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `core/primitives/src/receipt.rs` :: `is_instant_receipt`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: traffic timed so the target shard's chunk is missing for several heights; when receipt sizes sit exactly on the bandwidth-request granularity boundary; when the shard is driven exactly onto a congestion threshold
- Exploit idea: have congestion info extrapolated for missing chunks in a way that differs between nodes
- Invariant to test: congestion info for missing chunks is derived deterministically from the last present chunk
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test-loop test with forced missing chunks comparing derived congestion info
