# Q2898: bandwidth scheduler interaction with resharding — memtries.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, traffic saturating links across the exact block where the shard layout changes, when a referencing account is deleted while others still reference the code, and additionally when two account-creation paths race for the same id in one block, reach `delete_until_height` in `core/store/src/trie/mem/memtries.rs` and carry grants addressed to shard ids that no longer exist, stalling links permanently, breaking the invariant that scheduler state is remapped consistently at every layout change, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `core/store/src/trie/mem/memtries.rs` :: `delete_until_height`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: traffic saturating links across the exact block where the shard layout changes; when a referencing account is deleted while others still reference the code; when two account-creation paths race for the same id in one block
- Exploit idea: carry grants addressed to shard ids that no longer exist, stalling links permanently
- Invariant to test: scheduler state is remapped consistently at every layout change
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: test-loop test saturating links across a resharding boundary
