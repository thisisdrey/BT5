# Q2054: cross-shard receipt id derivation across layout versions — split.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts whose ids are derived at a layout boundary from attacker-chosen inputs, when a referencing account is deleted while others still reference the code, reach `nibbles_to_account_id` in `core/store/src/trie/split.rs` and produce two receipts with the same id in different shards so one is dropped as duplicate, breaking the invariant that receipt ids are globally unique across shards and layout versions, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/store/src/trie/split.rs` :: `nibbles_to_account_id`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts whose ids are derived at a layout boundary from attacker-chosen inputs; when a referencing account is deleted while others still reference the code
- Exploit idea: produce two receipts with the same id in different shards so one is dropped as duplicate
- Invariant to test: receipt ids are globally unique across shards and layout versions
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: unit test on receipt-id derivation across layout versions
