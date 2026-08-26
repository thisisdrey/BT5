# Q5440: delayed receipt queue index wraparound — congestion_control.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, sustained receipt pressure pushing the delayed-receipt indices toward their numeric bound, when the shard oscillates across the congestion threshold every block, and additionally when the target shard's chunk is missing for several consecutive heights, reach `get_receipt_group_sizes_for_buffer_to_shard` in `runtime/runtime/src/congestion_control.rs` and make the queue head/tail indices wrap so receipts are skipped or replayed, breaking the invariant that delayed receipt indices are monotone and never alias an already-processed slot, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/congestion_control.rs` :: `get_receipt_group_sizes_for_buffer_to_shard`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: sustained receipt pressure pushing the delayed-receipt indices toward their numeric bound; when the shard oscillates across the congestion threshold every block; when the target shard's chunk is missing for several consecutive heights
- Exploit idea: make the queue head/tail indices wrap so receipts are skipped or replayed
- Invariant to test: delayed receipt indices are monotone and never alias an already-processed slot
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: store test driving indices near the bound and asserting no aliasing
