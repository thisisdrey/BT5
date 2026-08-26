# Q5018: receipt buffering across a shard-layout change — outgoing_metadata.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, buffered receipts targeting a shard id that ceases to exist at the resharding boundary, when the shard is driven exactly onto a congestion threshold, and additionally when the shard oscillates across the congestion threshold every block, reach `iter_receipt_group_sizes` in `core/store/src/trie/outgoing_metadata.rs` and strand buffered value addressed to a retired shard, breaking the invariant that buffered receipts are remapped to the successor shard at layout changes, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `core/store/src/trie/outgoing_metadata.rs` :: `iter_receipt_group_sizes`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: buffered receipts targeting a shard id that ceases to exist at the resharding boundary; when the shard is driven exactly onto a congestion threshold; when the shard oscillates across the congestion threshold every block
- Exploit idea: strand buffered value addressed to a retired shard
- Invariant to test: buffered receipts are remapped to the successor shard at layout changes
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: test-loop test with receipts buffered across a resharding boundary
