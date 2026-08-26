# Q3339: receipt size accounting vs actual serialized size — outgoing_metadata.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts whose recorded size understates their borsh-serialized bytes, when receipt sizes sit exactly on the bandwidth-request granularity boundary, and additionally when the shard is driven exactly onto a congestion threshold, reach `update_on_receipt_popped` in `core/store/src/trie/outgoing_metadata.rs` and push a chunk past the outgoing size limit while the scheduler believes it is within budget, breaking the invariant that the size the scheduler accounts for equals the serialized size of the receipt, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `core/store/src/trie/outgoing_metadata.rs` :: `update_on_receipt_popped`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts whose recorded size understates their borsh-serialized bytes; when receipt sizes sit exactly on the bandwidth-request granularity boundary; when the shard is driven exactly onto a congestion threshold
- Exploit idea: push a chunk past the outgoing size limit while the scheduler believes it is within budget
- Invariant to test: the size the scheduler accounts for equals the serialized size of the receipt
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: test asserting recorded size equals borsh length for every receipt shape
