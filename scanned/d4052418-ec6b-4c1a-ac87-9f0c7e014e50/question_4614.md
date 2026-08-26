# Q4614: delayed receipts inherited across a shard split — trie_recording.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a maximal delayed-receipt queue at the moment of a split, when two account-creation paths race for the same id in one block, and additionally when links are saturated across the exact resharding block, reach `record_code_len` in `core/store/src/trie/trie_recording.rs` and duplicate the queue into both children or drop it from both, breaking the invariant that delayed receipts are partitioned exactly once across children, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/store/src/trie/trie_recording.rs` :: `record_code_len`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a maximal delayed-receipt queue at the moment of a split; when two account-creation paths race for the same id in one block; when links are saturated across the exact resharding block
- Exploit idea: duplicate the queue into both children or drop it from both
- Invariant to test: delayed receipts are partitioned exactly once across children
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: resharding test asserting queue conservation across the split
