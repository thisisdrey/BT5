# Q5126: cross-shard receipt ordering determinism — outgoing_metadata.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts created in one chunk that must be ordered before delivery to several shards, when the shard is driven exactly onto a congestion threshold, and additionally when the shard oscillates across the congestion threshold every block, reach `update_on_receipt_popped` in `core/store/src/trie/outgoing_metadata.rs` and make delivery order depend on a hash-map iteration rather than a canonical order, breaking the invariant that cross-shard receipt order is a canonical function of the producing chunk, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/store/src/trie/outgoing_metadata.rs` :: `update_on_receipt_popped`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts created in one chunk that must be ordered before delivery to several shards; when the shard is driven exactly onto a congestion threshold; when the shard oscillates across the congestion threshold every block
- Exploit idea: make delivery order depend on a hash-map iteration rather than a canonical order
- Invariant to test: cross-shard receipt order is a canonical function of the producing chunk
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test comparing delivery order across repeated runs
