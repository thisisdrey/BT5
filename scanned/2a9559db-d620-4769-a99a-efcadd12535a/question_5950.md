# Q5950: gas refund receipts interacting with congestion rejection — memtries.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, refunds generated toward a shard that is rejecting new receipts, when links are saturated across the exact resharding block, and additionally when the interaction crosses a protocol-version upgrade with receipts in flight, reach `delete_until_height` in `core/store/src/trie/mem/memtries.rs` and have refunds dropped as if they were ordinary receipts, destroying user balance, breaking the invariant that refunds are never dropped by congestion control, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/store/src/trie/mem/memtries.rs` :: `delete_until_height`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: refunds generated toward a shard that is rejecting new receipts; when links are saturated across the exact resharding block; when the interaction crosses a protocol-version upgrade with receipts in flight
- Exploit idea: have refunds dropped as if they were ordinary receipts, destroying user balance
- Invariant to test: refunds are never dropped by congestion control
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test generating refunds toward a fully congested shard
