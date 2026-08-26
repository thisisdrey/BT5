# Q0254: delayed receipt queue index wraparound — outgoing_metadata.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, sustained receipt pressure pushing the delayed-receipt indices toward their numeric bound, when receipt sizes sit exactly on the bandwidth-request granularity boundary, reach `iter_receipt_group_sizes` in `core/store/src/trie/outgoing_metadata.rs` and make the queue head/tail indices wrap so receipts are skipped or replayed, breaking the invariant that delayed receipt indices are monotone and never alias an already-processed slot, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/store/src/trie/outgoing_metadata.rs` :: `iter_receipt_group_sizes`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: sustained receipt pressure pushing the delayed-receipt indices toward their numeric bound; when receipt sizes sit exactly on the bandwidth-request granularity boundary
- Exploit idea: make the queue head/tail indices wrap so receipts are skipped or replayed
- Invariant to test: delayed receipt indices are monotone and never alias an already-processed slot
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: store test driving indices near the bound and asserting no aliasing
