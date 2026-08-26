# Q4912: protocol upgrade interacting with in-flight receipts — memtries.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts created under version N that execute in the first chunk of version N+1, when two account-creation paths race for the same id in one block, and additionally when links are saturated across the exact resharding block, reach `delete_snapshot` in `core/store/src/trie/mem/memtries.rs` and have the receipt priced or validated under a version that did not create it, breaking the invariant that in-flight receipts execute under well-defined, agreed-upon versioned rules, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/store/src/trie/mem/memtries.rs` :: `delete_snapshot`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts created under version N that execute in the first chunk of version N+1; when two account-creation paths race for the same id in one block; when links are saturated across the exact resharding block
- Exploit idea: have the receipt priced or validated under a version that did not create it
- Invariant to test: in-flight receipts execute under well-defined, agreed-upon versioned rules
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: test-loop test upgrading with receipts in flight
