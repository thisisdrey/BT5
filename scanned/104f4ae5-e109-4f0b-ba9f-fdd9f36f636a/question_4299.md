# Q4299: delayed receipt queue index wraparound — receipt.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, sustained receipt pressure pushing the delayed-receipt indices toward their numeric bound, when the shard is driven exactly onto a congestion threshold, and additionally when the shard oscillates across the congestion threshold every block, reach `receipt` in `core/primitives/src/receipt.rs` and make the queue head/tail indices wrap so receipts are skipped or replayed, breaking the invariant that delayed receipt indices are monotone and never alias an already-processed slot, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/receipt.rs` :: `receipt`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: sustained receipt pressure pushing the delayed-receipt indices toward their numeric bound; when the shard is driven exactly onto a congestion threshold; when the shard oscillates across the congestion threshold every block
- Exploit idea: make the queue head/tail indices wrap so receipts are skipped or replayed
- Invariant to test: delayed receipt indices are monotone and never alias an already-processed slot
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: store test driving indices near the bound and asserting no aliasing
