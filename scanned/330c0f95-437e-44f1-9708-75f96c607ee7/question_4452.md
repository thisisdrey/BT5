# Q4452: receipt-to-transaction backfill consistency — view_client_actor.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts whose originating transaction hash is attacker-influenced, when the pool is filled exactly to its bound by many attacker keys, and additionally when the same transaction is replayable across a reorg at the window edge, reach `record_receipt_to_tx_outcome` in `chain/client/src/view_client_actor.rs` and break the receipt-to-transaction mapping so outcomes are attributed to the wrong transaction, breaking the invariant that every receipt maps to exactly one originating transaction, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `chain/client/src/view_client_actor.rs` :: `record_receipt_to_tx_outcome`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts whose originating transaction hash is attacker-influenced; when the pool is filled exactly to its bound by many attacker keys; when the same transaction is replayable across a reorg at the window edge
- Exploit idea: break the receipt-to-transaction mapping so outcomes are attributed to the wrong transaction
- Invariant to test: every receipt maps to exactly one originating transaction
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test asserting the mapping over crafted receipt chains
