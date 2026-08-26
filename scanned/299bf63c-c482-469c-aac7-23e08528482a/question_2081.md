# Q2081: cross-shard receipt ordering determinism — congestion_info.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts created in one chunk that must be ordered before delivery to several shards, when receipt sizes sit exactly on the bandwidth-request granularity boundary, reach `add_receipt_bytes` in `core/primitives/src/congestion_info.rs` and make delivery order depend on a hash-map iteration rather than a canonical order, breaking the invariant that cross-shard receipt order is a canonical function of the producing chunk, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives/src/congestion_info.rs` :: `add_receipt_bytes`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts created in one chunk that must be ordered before delivery to several shards; when receipt sizes sit exactly on the bandwidth-request granularity boundary
- Exploit idea: make delivery order depend on a hash-map iteration rather than a canonical order
- Invariant to test: cross-shard receipt order is a canonical function of the producing chunk
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test comparing delivery order across repeated runs
