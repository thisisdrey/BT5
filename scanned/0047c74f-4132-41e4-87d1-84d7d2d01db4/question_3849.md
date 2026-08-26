# Q3849: cross-shard receipt ordering determinism — distribute_remaining.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts created in one chunk that must be ordered before delivery to several shards, when receipt sizes sit exactly on the bandwidth-request granularity boundary, and additionally when the shard is driven exactly onto a congestion threshold, reach `average_link_bandwidth` in `runtime/runtime/src/bandwidth_scheduler/distribute_remaining.rs` and make delivery order depend on a hash-map iteration rather than a canonical order, breaking the invariant that cross-shard receipt order is a canonical function of the producing chunk, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/runtime/src/bandwidth_scheduler/distribute_remaining.rs` :: `average_link_bandwidth`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts created in one chunk that must be ordered before delivery to several shards; when receipt sizes sit exactly on the bandwidth-request granularity boundary; when the shard is driven exactly onto a congestion threshold
- Exploit idea: make delivery order depend on a hash-map iteration rather than a canonical order
- Invariant to test: cross-shard receipt order is a canonical function of the producing chunk
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test comparing delivery order across repeated runs
